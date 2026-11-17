"""Timeline reference model (independent oracle) for benefit replay.

The model is *event-sourced* and keeps four time concepts strictly apart:

1. **application time** (``at`` on an application event): when the household
   applied for a benefit period.
2. **evidence effective time** (``at`` on evidence / income / household
   events): the point in time from which a fact is in force.
3. **decision time** (``at`` on a decision request): when the agency is asked
   to (re)decide a period.
4. **notification time** (``notify_at`` on a decision request): when the
   resulting decision version is dispatched.  A dispatched version is frozen;
   later changes may only create new adjustment lines, never mutate it.

Time is an integer day ordinal.  All monetary amounts are integer cents, so
no floating-point rounding can leak into the conservation properties.

The reference implementation is deliberately written from the regulatory
rules below rather than from the production code, and it is fully
deterministic.  Every externally observable step goes through a swappable
:class:`Components` object, which is what the fault-injection layer mutates.

Rules encoded here
------------------
* Events are de-duplicated by a stable natural key; repeated delivery of the
  same event is a no-op.
* A correction whose effective time is later than the decision time cannot
  influence that decision; an invalid (malformed) correction is rejected.
* The policy used for a decision is the version in force at **evidence
  effective time AND already enacted at decision time** (a policy may be
  enacted retroactively, but it never reaches decisions made before its
  enactment).  Retroactivity only affects periods inside its effective range
  and only decisions that have not been notified yet.
* An application needs application evidence; decisions lacking required
  evidence are ``pending`` (benefit 0).
* Eligibility: household size >= 1 and total household income <=
  household size * income_limit_per_person.
* A previously approved period that is no longer eligible is ``suspended``;
  becoming eligible again yields ``resumed``.
* Partial-month benefit is prorated per eligible day and rounded half up to
  whole cents; exact-boundary income and zero-member households are explicit.
* Appeals may overturn a decided period.  Overpayments become ``clawback``
  adjustments and underpayments become ``makeup`` adjustments, so the ledger
  always balances: *notified + adjustments = final entitlement*.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Event taxonomy
# ---------------------------------------------------------------------------
EVENT_APPLICATION = "application"
EVENT_INCOME = "income"
EVENT_HOUSEHOLD = "household"
EVENT_EVIDENCE = "evidence"
EVENT_POLICY = "policy"

ALL_EVENTS = (EVENT_APPLICATION, EVENT_INCOME, EVENT_HOUSEHOLD, EVENT_EVIDENCE, EVENT_POLICY)

# Decision statuses (state machine).
STATUS_APPROVED = "approved"
STATUS_PENDING = "pending"
STATUS_DENIED = "denied"
STATUS_SUSPENDED = "suspended"
STATUS_RESUMED = "resumed"
STATUS_OVERTURNED = "overturned"

# Adjustment directions.
ADJ_MAKEUP = "makeup"        # agency underpaid -> pays more
ADJ_CLAWBACK = "clawback"    # agency overpaid -> recovers

MONTH_DAYS = 30


@dataclass(frozen=True)
class Event:
    """A single immutable timeline event.

    ``at`` is the evidence/application effective time.  ``delivered_at`` is
    the intake time used for duplicate-delivery bookkeeping; it is never used
    for eligibility comparisons.
    """

    event_id: str
    case: str
    kind: str
    at: int
    payload: dict = field(default_factory=dict)
    delivered_at: Optional[int] = None
    valid: bool = True

    def natural_key(self) -> tuple:
        """Stable identity used for de-duplication of repeated delivery.

        Household/income corrections carry their payload in the key: two
        distinct corrections sharing one effective time are *not* duplicates
        (they are resolved deterministically by event id), whereas an exact
        redelivery – same kind, time and payload – is.
        """
        if self.kind == EVENT_POLICY:
            # Policy versions are identified by their effective range.
            return (EVENT_POLICY, self.payload.get("policy_id"), self.at)
        if self.kind == EVENT_EVIDENCE:
            # Evidence identity is the document itself: redelivery of the
            # same document is a duplicate even if it gets a new intake id.
            return (EVENT_EVIDENCE, self.case, self.at,
                    self.payload.get("doc", ""))
        if self.kind in (EVENT_INCOME, EVENT_HOUSEHOLD):
            # Same kind/case/effective time is the same correction slot;
            # distinct simultaneous corrections are resolved deterministically
            # by event id in _facts_at (last-writer-wins), and a redelivery
            # that mutates the payload is still a duplicate of that slot.
            return (self.kind, self.case, self.at)
        return (self.kind, self.case, self.at)


@dataclass(frozen=True)
class Policy:
    policy_id: str
    effective_from: int
    effective_to: Optional[int]      # inclusive; None means open-ended
    enacted_at: int                  # first day the agency may apply it
    income_limit_per_person: int     # cents
    benefit_monthly: int             # cents per full 30-day month, per case

    def in_force_at(self, day: int) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to


@dataclass(frozen=True)
class DecisionRequest:
    """One agency decision activity: distinct decision/notification times."""

    case: str
    period: int                      # benefit period ordinal (month index)
    period_start: int
    period_end: int                  # inclusive
    at: int                          # decision time
    notify_at: Optional[int] = None  # notification time; None = never sent
    appeal: bool = False             # appeal against the latest version


@dataclass(frozen=True)
class Case:
    case: str
    application_at: int


@dataclass(frozen=True)
class Timeline:
    cases: tuple[Case, ...]
    events: tuple[Event, ...]
    policies: tuple[Policy, ...]
    requests: tuple[DecisionRequest, ...]

    # ----- immutable derivations / transformations -----------------------
    def with_events(self, events) -> "Timeline":
        return replace(self, events=tuple(events))

    def with_requests(self, requests) -> "Timeline":
        return replace(self, requests=tuple(requests))

    def with_policies(self, policies) -> "Timeline":
        return replace(self, policies=tuple(policies))

    def with_cases(self, cases) -> "Timeline":
        return replace(self, cases=tuple(cases))

    def cutoff(self, day: int) -> "Timeline":
        """Return the prefix of the timeline observable up to ``day``.

        Events effective after ``day`` and requests decided/notify after
        ``day`` are removed.  Used to prove frozen notifications never change
        when "the future" is replayed.
        """
        return Timeline(
            cases=self.cases,
            events=tuple(e for e in self.events if e.at <= day),
            policies=tuple(
                p for p in self.policies
                if p.effective_from <= day and p.enacted_at <= day
            ),
            requests=tuple(r for r in self.requests if r.at <= day),
        )


# ---------------------------------------------------------------------------
# Decision / ledger records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DecisionVersion:
    case: str
    period: int
    version: int
    status: str
    decided_at: int
    policy_id: Optional[str]
    income: int
    household_size: int
    limit: int
    eligible_days: int
    benefit_cents: int          # exact entitlement for this version
    evidence: tuple[str, ...]
    reason: str

    # Evidence attachments are diagnostic metadata: adding or re-delivering a
    # supporting document must not, by itself, make the decision "different".
    # Equality therefore covers every decision-semantic field but evidence.
    def _identity(self):
        return (
            self.case, self.period, self.version, self.status,
            self.decided_at, self.policy_id, self.income,
            self.household_size, self.limit, self.eligible_days,
            self.benefit_cents, self.reason,
        )

    def __eq__(self, other):
        if not isinstance(other, DecisionVersion):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self):
        return hash(self._identity())


@dataclass(frozen=True)
class Notification:
    case: str
    period: int
    version: int
    notified_at: int
    benefit_cents: int          # snapshot, frozen forever
    status: str


@dataclass(frozen=True)
class Adjustment:
    case: str
    period: int
    kind: str                   # makeup | clawback
    amount_cents: int           # always positive
    caused_by_request: int      # index into requests
    reason: str


@dataclass(frozen=True)
class ReplayResult:
    decisions: tuple[DecisionVersion, ...]
    notifications: tuple[Notification, ...]
    adjustments: tuple[Adjustment, ...]
    rejected_events: tuple           # natural keys; order-independent
    duplicate_events: tuple          # natural keys; order-independent
    requests: tuple[DecisionRequest, ...]
    appeals_upheld: tuple[int, ...]
    faulty_report: Optional[list] = None  # fault-injection seam only

    def __post_init__(self):
        # Bookkeeping sets are stored as sorted key tuples so the whole
        # result is comparable regardless of intake/delivery ordering.
        object.__setattr__(
            self, "rejected_events",
            tuple(sorted(self.rejected_events)),
        )
        object.__setattr__(
            self, "duplicate_events",
            tuple(sorted(self.duplicate_events)),
        )

    # ----- reporting views ------------------------------------------------
    def latest_versions(self) -> dict[tuple[str, int], DecisionVersion]:
        latest: dict[tuple[str, int], DecisionVersion] = {}
        for d in self.decisions:
            latest[(d.case, d.period)] = d
        return latest

    def report_rows(self) -> list[dict]:
        """Stable field-mapped reporting envelope (mapping is under test)."""
        if self.faulty_report is not None:
            return [dict(row) for row in self.faulty_report]
        latest = self.latest_versions()
        notified: dict[tuple[str, int], Notification] = {
            (n.case, n.period): n for n in self.notifications
        }
        rows = []
        for (case, period), decision in sorted(latest.items()):
            note = notified.get((case, period))
            rows.append({
                "case": case,
                "period": period,
                "final_status": decision.status,
                "final_entitlement_cents": decision.benefit_cents,
                "decided_at": decision.decided_at,
                "policy_id": decision.policy_id,
                "notified": note is not None,
                "notified_at": note.notified_at if note else None,
                "frozen_notified_cents": note.benefit_cents if note else None,
            })
        return rows


# ---------------------------------------------------------------------------
# Swappable components (fault-injection seams)
# ---------------------------------------------------------------------------
def default_dedup(events):
    """De-duplicate repeated delivery by stable natural key.

    The active stream keeps the first occurrence of each valid natural key.
    The duplicate set is defined *symmetrically* – every natural key that
    occurs more than once in the intake – so bookkeeping is independent of
    delivery order.  Invalid (malformed) corrections are excluded from dedup:
    they are always rejected at intake, never silently swallowed.
    """
    occurrences: dict[tuple, int] = {}
    seen: dict[tuple, Event] = {}
    unique: list[Event] = []
    for event in events:
        if not event.valid:
            unique.append(event)
            continue
        key = event.natural_key()
        occurrences[key] = occurrences.get(key, 0) + 1
        if key not in seen:
            seen[key] = event
            unique.append(event)
    duplicates = tuple(sorted(key for key, count in occurrences.items()
                              if count > 1))
    return unique, duplicates


def default_policy_selector(policies, evidence_day, decision_day):
    """Policy in force at evidence time and already enacted at decision time."""
    candidates = [
        p for p in policies
        if p.in_force_at(evidence_day) and p.enacted_at <= decision_day
    ]
    if not candidates:
        return None
    # Deterministic tie-break: most recently enacted, then id.
    return sorted(candidates, key=lambda p: (-p.enacted_at, p.policy_id))[0]


def default_round_half_up(numerator: int, denominator: int) -> int:
    """Round numerator/denominator to the nearest integer, halves go up."""
    assert denominator > 0
    q, r = divmod(abs(numerator), denominator)
    rounded = q + (1 if 2 * r >= denominator else 0)
    return rounded if numerator >= 0 else -rounded


def default_evidence_check(has_application_evidence: bool) -> bool:
    return has_application_evidence


@dataclass(frozen=True)
class Components:
    """Bundle of swappable oracle steps.  Defaults are the correct rules."""

    dedup: Callable = default_dedup
    policy_selector: Callable = default_policy_selector
    rounder: Callable = default_round_half_up
    evidence_check: Callable = default_evidence_check


# ---------------------------------------------------------------------------
# Core replay
# ---------------------------------------------------------------------------
def _facts_at(events, case, day):
    """Household facts valid at evidence-effective time ``day``.

    Only events with ``at <= day`` count; an event's effective time is never
    compared against intake/delivery time.  Income events are additive
    streams; household events are state corrections, so when several
    corrections share the same effective time the one with the greatest
    event id deterministically wins (last-writer-wins), independent of
    delivery order.
    """
    income = 0
    household_size = 0
    evidence_docs: list[str] = []
    has_application = False
    household_winner = None
    for event in events:
        if event.case != case or event.at > day or not event.valid:
            continue
        if event.kind == EVENT_INCOME:
            income += int(event.payload["amount_cents"])
        elif event.kind == EVENT_HOUSEHOLD:
            if household_winner is None or (event.at, event.event_id) > (
                household_winner.at, household_winner.event_id
            ):
                household_winner = event
        elif event.kind == EVENT_EVIDENCE:
            evidence_docs.append(str(event.payload.get("doc", "")))
        elif event.kind == EVENT_APPLICATION:
            has_application = True
    if household_winner is not None:
        household_size = int(household_winner.payload["members"])
    return income, household_size, tuple(sorted(evidence_docs)), has_application


def _evaluate(request, case_events, policies, components: Components):
    """Pure eligibility evaluation for one decision request.

    Two distinct reference times are used on purpose:

    * ``period_start`` selects the policy version whose effective range
      covers the benefit period (the policy's evidence-effective anchor);
    * ``request.at`` (decision time) bounds which household facts and
      evidence the agency can lawfully consider.  A correction effective
      after the decision time does not exist for that decision.
    """
    policy_anchor = request.period_start
    income, household_size, docs, has_application = _facts_at(
        case_events, request.case, request.at
    )
    policy = components.policy_selector(
        policies, policy_anchor, request.at
    )

    if not components.evidence_check(has_application):
        return {
            "status": STATUS_PENDING, "policy": policy, "income": income,
            "household_size": household_size, "limit": 0,
            "eligible_days": 0, "benefit": 0,
            "evidence": docs, "reason": "missing_application_evidence",
        }
    if policy is None:
        return {
            "status": STATUS_PENDING, "policy": policy, "income": income,
            "household_size": household_size, "limit": 0,
            "eligible_days": 0, "benefit": 0,
            "evidence": docs, "reason": "no_enacted_policy",
        }

    limit = household_size * policy.income_limit_per_person
    # Structural ineligibility (empty household) is evaluated before the
    # income comparison so its reason is never masked by the limit check.
    if household_size < 1:
        return {
            "status": STATUS_DENIED, "policy": policy, "income": income,
            "household_size": household_size, "limit": limit,
            "eligible_days": 0, "benefit": 0,
            "evidence": docs, "reason": "empty_household",
        }
    eligible = income <= limit
    if not eligible:
        return {
            "status": STATUS_DENIED, "policy": policy, "income": income,
            "household_size": household_size, "limit": limit,
            "eligible_days": 0, "benefit": 0,
            "evidence": docs, "reason": "income_over_limit",
        }

    eligible_days = request.period_end - request.period_start + 1
    benefit = components.rounder(
        policy.benefit_monthly * eligible_days, MONTH_DAYS
    )
    return {
        "status": STATUS_APPROVED, "policy": policy, "income": income,
        "household_size": household_size, "limit": limit,
        "eligible_days": eligible_days, "benefit": benefit,
        "evidence": docs, "reason": "eligible",
    }


def replay(timeline: Timeline, components: Optional[Components] = None) -> ReplayResult:
    """Replay the whole timeline deterministically and return the ledger."""
    components = components or Components()

    unique, duplicates = components.dedup(timeline.events)
    # Invalid corrections are rejected at intake and never enter evaluation.
    rejected = tuple(sorted(e.natural_key() for e in unique if not e.valid))
    active = [e for e in unique if e.valid]

    by_case: dict[str, list[Event]] = {}
    for event in active:
        by_case.setdefault(event.case, []).append(event)
    for evs in by_case.values():
        evs.sort(key=lambda e: (e.at, e.event_id))

    decisions: list[DecisionVersion] = []
    notifications: list[Notification] = []
    adjustments: list[Adjustment] = []
    appeals_upheld: list[int] = []
    # Current version counter and last status per (case, period).
    versions: dict[tuple[str, int], int] = {}
    last_status: dict[tuple[str, int], str] = {}
    notified_keys: set[tuple[str, int]] = set()

    for idx, request in enumerate(timeline.requests):
        key = (request.case, request.period)
        prior = _last_version(decisions, request.case, request.period)

        if request.appeal:
            if prior is None:
                continue  # nothing to appeal against; ignore malformed request
            if prior.status == STATUS_PENDING:
                continue  # an undecided case has no verdict to overturn
            outcome = _evaluate(
                request, by_case.get(request.case, []),
                timeline.policies, components
            )
            # An appeal is upheld when the re-evaluation reaches a different
            # eligibility verdict from the decision under appeal, in either
            # direction (denial->approval or approval->denial).
            prior_eligible = prior.status in (
                STATUS_APPROVED, STATUS_RESUMED, STATUS_OVERTURNED
            )
            fresh_eligible = outcome["status"] == STATUS_APPROVED
            if fresh_eligible != prior_eligible:
                appeals_upheld.append(idx)
                new_version = versions.get(key, 0) + 1
                versions[key] = new_version
                decision = DecisionVersion(
                    case=request.case, period=request.period,
                    version=new_version, status=STATUS_OVERTURNED,
                    decided_at=request.at,
                    policy_id=outcome["policy"].policy_id,
                    income=outcome["income"],
                    household_size=outcome["household_size"],
                    limit=outcome["limit"],
                    eligible_days=outcome["eligible_days"],
                    benefit_cents=outcome["benefit"],
                    evidence=("appeal",) + outcome["evidence"],
                    reason="appeal_upheld",
                )
                decisions.append(decision)
                # Conservation: reconcile against the frozen notification.
                _emit_appeal_adjustment(
                    request, idx, prior, decision,
                    notified_keys, notifications, adjustments
                )
                last_status[key] = STATUS_OVERTURNED
            else:
                appeals_upheld  # appeal rejected: state unchanged
            continue

        outcome = _evaluate(
            request, by_case.get(request.case, []),
            timeline.policies, components
        )
        status = outcome["status"]
        if status == STATUS_APPROVED and prior is not None:
            if prior.status in (STATUS_DENIED, STATUS_SUSPENDED):
                status = STATUS_RESUMED
        elif status == STATUS_DENIED and prior is not None:
            if prior.status in (STATUS_APPROVED, STATUS_RESUMED, STATUS_OVERTURNED):
                status = STATUS_SUSPENDED

        new_version = versions.get(key, 0) + 1
        versions[key] = new_version
        policy = outcome["policy"]
        decision = DecisionVersion(
            case=request.case, period=request.period, version=new_version,
            status=status, decided_at=request.at,
            policy_id=policy.policy_id if policy else None,
            income=outcome["income"],
            household_size=outcome["household_size"],
            limit=outcome["limit"], eligible_days=outcome["eligible_days"],
            benefit_cents=outcome["benefit"],
            evidence=outcome["evidence"], reason=outcome["reason"],
        )
        decisions.append(decision)
        last_status[key] = status

        if request.notify_at is not None:
            notifications.append(Notification(
                case=request.case, period=request.period,
                version=new_version, notified_at=request.notify_at,
                benefit_cents=decision.benefit_cents, status=status,
            ))
            notified_keys.add(key)

    return ReplayResult(
        decisions=tuple(decisions),
        notifications=tuple(notifications),
        adjustments=tuple(adjustments),
        rejected_events=tuple(rejected),
        duplicate_events=tuple(duplicates),
        requests=tuple(timeline.requests),
        appeals_upheld=tuple(appeals_upheld),
    )


def _last_version(decisions, case, period):
    found = None
    for decision in decisions:
        if decision.case == case and decision.period == period:
            found = decision
    return found


def _emit_appeal_adjustment(request, idx, prior, decision,
                            notified_keys, notifications, adjustments):
    """Appeal money movement preserves the conservation identity.

    The adjustment reconciles the *frozen notified snapshot* for this
    (case, period) against the new entitlement – not the prior version,
    whose amount may differ from what was actually dispatched.  A zero
    delta needs no monetary movement.
    """
    key = (request.case, request.period)
    if key not in notified_keys:
        return
    notified = next(n for n in notifications
                    if n.case == key[0] and n.period == key[1])
    delta = decision.benefit_cents - notified.benefit_cents
    if delta > 0:
        adjustments.append(Adjustment(
            request.case, request.period, ADJ_MAKEUP, delta, idx,
            "appeal_overturned_denial_makeup",
        ))
    elif delta < 0:
        adjustments.append(Adjustment(
            request.case, request.period, ADJ_CLAWBACK, -delta, idx,
            "appeal_overturned_approval_clawback",
        ))
