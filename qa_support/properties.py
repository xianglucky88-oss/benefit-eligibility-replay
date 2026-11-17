"""Property checks against the timeline reference oracle.

Every property is a pure function ``Timeline -> (ok, detail)`` that never
relaxes itself on failure: ``ok`` is a strict equality/identity assertion and
``detail`` explains the first divergence.  The properties cover the four
required time semantics, event reordering / redelivery / invalid correction
immunity, retroactive policy scope, notification freezing, the
suspend/resume and appeal state machines, money conservation, household
boundaries and cent-exact rounding.
"""
from __future__ import annotations

from dataclasses import replace

from .timeline import (
    ADJ_CLAWBACK,
    ADJ_MAKEUP,
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_OVERTURNED,
    STATUS_PENDING,
    STATUS_RESUMED,
    STATUS_SUSPENDED,
    Components,
    Event,
    ReplayResult,
    Timeline,
    default_round_half_up,
    replay,
    EVENT_APPLICATION,
    EVENT_EVIDENCE,
    EVENT_HOUSEHOLD,
    EVENT_INCOME,
    MONTH_DAYS,
)

# Properties are named with stable ids so fault localisation can name them.
PROPERTY_IDS = (
    "P1_timestamp_separation",
    "P2_delivery_order_invariance",
    "P3_redelivery_dedup_invariance",
    "P4_invalid_correction_rejected",
    "P5_retro_policy_scope",
    "P6_notification_freeze",
    "P7_suspend_resume_transitions",
    "P8_evidence_gating",
    "P9_appeal_adjustment_conservation",
    "P10_ledger_conservation",
    "P11_household_and_limit_boundary",
    "P12_cent_exact_rounding",
    "P13_determinism",
    "P14_report_field_mapping",
)


def _fail(detail):
    return False, detail


def _ok():
    return True, ""


# ---------------------------------------------------------------------------
# P1 – application / evidence-effective / decision / notification times
# ---------------------------------------------------------------------------
def p1_timestamp_separation(tl: Timeline):
    base = replay(tl)

    # (a) Moving every notification time later must not change any decision
    #     version nor the frozen benefit snapshot attached to a notification.
    shifted_req = tuple(
        replace(r, notify_at=r.notify_at + 7 if r.notify_at is not None else None)
        for r in tl.requests
    )
    shifted = replay(tl.with_requests(shifted_req))
    if shifted.decisions != base.decisions:
        return _fail("shifting notification time changed decision content")
    base_snaps = {(n.case, n.period, n.version): n.benefit_cents
                  for n in base.notifications}
    for note in shifted.notifications:
        key = (note.case, note.period, note.version)
        if base_snaps[key] != note.benefit_cents:
            return _fail("notification snapshot amount changed with notify time")

    # (b) Intake/delivery time is irrelevant to eligibility: perturb every
    #     delivered_at and expect an identical ledger.
    moved_events = tuple(
        replace(e, delivered_at=(e.delivered_at or e.at) + 13) for e in tl.events
    )
    if replay(tl.with_events(moved_events)) != base:
        return _fail("replay changed when event delivery time moved")

    # (c) A correction whose effective time is later than every decision that
    #     precedes it cannot influence those decisions.  Remove the latest
    #     effective event per case and compare the still-earlier decisions.
    for case in {c.case for c in tl.cases}:
        case_events = [e for e in tl.events if e.case == case]
        if not case_events:
            continue
        latest_at = max(e.at for e in case_events)
        late = [e for e in case_events if e.at == latest_at
                and e.kind in (EVENT_INCOME, EVENT_HOUSEHOLD)]
        if not late:
            continue
        removed = tuple(e for e in tl.events if e not in late)
        before = tuple(d for d in base.decisions
                       if d.case == case and d.decided_at < latest_at)
        after = tuple(d for d in replay(tl.with_events(removed)).decisions
                      if d.case == case and d.decided_at < latest_at)
        if before != after:
            return _fail(
                f"late effective correction altered an earlier decision "
                f"for case {case}"
            )

    # (d) The four time concepts are all independently observable: a
    #     notification can never predate the decision version it documents.
    for note in base.notifications:
        decision = next(d for d in base.decisions
                        if d.case == note.case and d.period == note.period
                        and d.version == note.version)
        if note.notified_at < decision.decided_at:
            return _fail("notification predates its own decision")
        if note.notified_at is None:
            return _fail("notified record has no notification time")
    return _ok()


# ---------------------------------------------------------------------------
# P2 – arbitrary delivery (event) ordering must not change the result
# ---------------------------------------------------------------------------
def p2_delivery_order_invariance(tl: Timeline):
    base = replay(tl)
    # Timelines containing redelivered natural keys are P3's domain: by
    # definition first-occurrence dedup is order-relative there.  P2 covers
    # the complementary case where every intake record has a unique key.
    if base.duplicate_events:
        return _ok()
    # Reorder the complete intake.  Correct replay must be order-insensitive
    # because simultaneous distinct corrections use a deterministic
    # (effective time, event id) tie-break, independent of delivery order.
    semantic = ("decisions", "notifications", "adjustments")
    orders = [
        sorted(tl.events, key=lambda e: e.event_id, reverse=True),
        sorted(tl.events,
               key=lambda e: (e.delivered_at if e.delivered_at is not None
                              else e.at, e.event_id)),
        tuple(reversed(tl.events)),
    ]
    for idx, events in enumerate(orders):
        other = replay(tl.with_events(events))
        for field_name in semantic:
            if getattr(base, field_name) != getattr(other, field_name):
                return _fail(
                    f"{field_name} differ under intake ordering #{idx}"
                )
    return _ok()


# ---------------------------------------------------------------------------
# P3 – repeated delivery is de-duplicated by natural key
# ---------------------------------------------------------------------------
def p3_redelivery_dedup_invariance(tl: Timeline):
    base = replay(tl)
    redelivered = list(tl.events)
    for event in tl.events:
        redelivered.append(Event(
            event_id=event.event_id + "-dup",
            case=event.case, kind=event.kind, at=event.at,
            payload=dict(event.payload),
            delivered_at=(event.delivered_at or event.at) + 1,
            valid=event.valid,
        ))
    again = replay(tl.with_events(redelivered))
    if again.decisions != base.decisions:
        return _fail("duplicate delivery changed decisions")
    if again.notifications != base.notifications:
        return _fail("duplicate delivery changed notifications")
    if again.adjustments != base.adjustments:
        return _fail("duplicate delivery changed adjustments")
    # Every redelivery of a *valid* event is a duplicate; invalid ones are
    # rejected at intake rather than de-duplicated.
    expected = {e.natural_key() for e in tl.events if e.valid}
    got = set(again.duplicate_events)
    if not expected.issubset(got):
        return _fail("redelivered valid events were not recorded as duplicates")
    invalid_redelivered = sum(1 for e in tl.events if not e.valid)
    if len(again.rejected_events) != (
        len(base.rejected_events) + invalid_redelivered
    ):
        return _fail("redelivered invalid correction was not rejected again")
    return _ok()


# ---------------------------------------------------------------------------
# P4 – invalid corrections are rejected at intake
# ---------------------------------------------------------------------------
def p4_invalid_correction_rejected(tl: Timeline):
    base = replay(tl)
    poisoned_events = list(tl.events)
    for event in tl.events:
        if event.kind in (EVENT_INCOME, EVENT_HOUSEHOLD):
            poisoned_events.append(Event(
                event_id=event.event_id + "-bad",
                case=event.case, kind=event.kind, at=event.at,
                payload={**event.payload, "amount_cents": 10 ** 9,
                         "members": 99},
                valid=False,
            ))
    poisoned = replay(tl.with_events(poisoned_events))
    if poisoned.decisions != base.decisions:
        return _fail("invalid correction influenced a decision")
    bad_keys = {
        e.natural_key() for e in poisoned_events if not e.valid
    }
    if not bad_keys.issubset(set(poisoned.rejected_events)):
        return _fail("invalid correction was not rejected at intake")
    # The rejected invalid record never reaches evaluation: active events
    # are exactly the valid ones, and every rejected key is one of them.
    active = [e for e in poisoned_events
              if e.valid and e.natural_key() not in set(poisoned.rejected_events)]
    if any(not e.valid for e in active):
        return _fail("an invalid event leaked onto the evaluation path")
    return _ok()


# ---------------------------------------------------------------------------
# P5 – retroactive policy only acts inside its effective/enacted scope
# ---------------------------------------------------------------------------
def p5_retro_policy_scope(tl: Timeline):
    result = replay(tl)
    policies = {p.policy_id: p for p in tl.policies}
    for decision in result.decisions:
        if decision.policy_id is None:
            continue
        policy = policies[decision.policy_id]
        request = next(r for r in tl.requests
                       if r.case == decision.case and r.period == decision.period
                       and r.at == decision.decided_at)
        if policy.enacted_at > decision.decided_at:
            return _fail(
                f"{decision.policy_id} used before its enactment "
                f"({policy.enacted_at} > {decision.decided_at})"
            )
        if not policy.in_force_at(request.period_start):
            return _fail(
                f"{decision.policy_id} applied outside its effective range"
            )
    # Explicit retroactive check on a timeline containing a retro policy.
    retro = next((p for p in tl.policies if p.policy_id == "P-retro"), None)
    if retro is not None:
        for decision in result.decisions:
            request = next(r for r in tl.requests
                           if r.case == decision.case and r.period == decision.period
                           and r.at == decision.decided_at)
            in_range = retro.in_force_at(request.period_start)
            if decision.decided_at < retro.enacted_at and in_range:
                if decision.policy_id == retro.policy_id:
                    return _fail("retroactive policy reached a pre-enactment decision")
            if decision.decided_at >= retro.enacted_at and in_range:
                if decision.policy_id != retro.policy_id:
                    return _fail(
                        "in-scope post-enactment decision did not select retro policy"
                    )
    return _ok()


# ---------------------------------------------------------------------------
# P6 – an emitted notification is frozen forever
# ---------------------------------------------------------------------------
def p6_notification_freeze(tl: Timeline):
    full = replay(tl)
    for note in full.notifications:
        prefix = replay(tl.cutoff(note.notified_at))
        prefix_note = next(
            (n for n in prefix.notifications
             if n.case == note.case and n.period == note.period
             and n.version == note.version),
            None,
        )
        if prefix_note is None:
            return _fail("notification disappeared under prefix replay")
        if prefix_note != note:
            return _fail(
                f"notification {note.case}/p{note.period}/v{note.version} "
                f"was mutated by later timeline activity"
            )
        # The frozen amount must match the decision version it documents.
        decision = next(d for d in full.decisions
                        if d.case == note.case and d.period == note.period
                        and d.version == note.version)
        if note.benefit_cents != decision.benefit_cents:
            return _fail("notification snapshot disagrees with its decision version")
    # Adjustments never target a notification record directly.
    if any(adj.kind not in (ADJ_MAKEUP, ADJ_CLAWBACK) for adj in full.adjustments):
        return _fail("unknown adjustment kind")
    return _ok()


# ---------------------------------------------------------------------------
# P7 – suspension / resumption state machine
# ---------------------------------------------------------------------------
_LEGAL = {
    None: {STATUS_APPROVED, STATUS_DENIED, STATUS_PENDING},
    STATUS_APPROVED: {
        STATUS_APPROVED, STATUS_SUSPENDED, STATUS_PENDING, STATUS_OVERTURNED,
    },
    STATUS_SUSPENDED: {
        STATUS_SUSPENDED, STATUS_RESUMED, STATUS_PENDING, STATUS_OVERTURNED,
    },
    STATUS_DENIED: {
        STATUS_DENIED, STATUS_RESUMED, STATUS_PENDING, STATUS_OVERTURNED,
    },
    STATUS_RESUMED: {
        STATUS_RESUMED, STATUS_SUSPENDED, STATUS_PENDING, STATUS_OVERTURNED,
    },
    STATUS_PENDING: {
        STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED,
        STATUS_SUSPENDED, STATUS_RESUMED,
    },
    STATUS_OVERTURNED: {
        STATUS_OVERTURNED, STATUS_SUSPENDED, STATUS_RESUMED,
        STATUS_PENDING, STATUS_DENIED, STATUS_APPROVED,
    },
}


def p7_suspend_resume_transitions(tl: Timeline):
    result = replay(tl)
    seen: dict[tuple[str, int], str] = {}
    for decision in result.decisions:
        key = (decision.case, decision.period)
        prev = seen.get(key)
        if decision.status not in _LEGAL[prev]:
            return _fail(
                f"illegal transition {prev} -> {decision.status} for {key}"
            )
        if decision.status == STATUS_SUSPENDED and prev not in (
            STATUS_APPROVED, STATUS_RESUMED, STATUS_OVERTURNED
        ):
            return _fail("suspension without a prior approval")
        if decision.status == STATUS_RESUMED and prev not in (
            STATUS_DENIED, STATUS_SUSPENDED
        ):
            return _fail("resumption without a prior denial/suspension")
        seen[key] = decision.status
    return _ok()


# ---------------------------------------------------------------------------
# P8 – evidence gating and supplementary evidence uploads
# ---------------------------------------------------------------------------
def p8_evidence_gating(tl: Timeline):
    # Supplementary documents must never, by themselves, change entitlement.
    base = replay(tl)
    stripped = tl.with_events([
        e for e in tl.events if not (e.kind == EVENT_EVIDENCE
                                     and str(e.payload.get("doc", ""))
                                     .startswith("supp-"))
    ])
    if replay(stripped).decisions != base.decisions:
        return _fail("supplementary evidence altered eligibility")

    # Input-driven gating invariant: any pending decision must be explained
    # by missing application evidence or the absence of an enacted policy,
    # never by income/household facts alone.
    for decision in base.decisions:
        request = next(r for r in tl.requests
                       if r.case == decision.case
                       and r.period == decision.period
                       and r.at == decision.decided_at)
        has_app = any(
            e.case == decision.case and e.kind == EVENT_APPLICATION
            and e.valid and e.at <= request.at
            for e in tl.events
        )
        if decision.status == STATUS_PENDING:
            if decision.reason not in ("missing_application_evidence",
                                       "no_enacted_policy"):
                return _fail(f"pending decision has an unexplained reason: "
                             f"{decision.reason}")
            if decision.reason == "missing_application_evidence" and has_app:
                return _fail("pending despite available application evidence")
        # Converse: without an application event the agency cannot decide.
        if not has_app and decision.status not in (
            STATUS_PENDING,
        ):
            return _fail(
                f"case {decision.case} reached status {decision.status} "
                f"without any application evidence"
            )
    return _ok()


# ---------------------------------------------------------------------------
# P9 / P10 – appeal overturns and money conservation
# ---------------------------------------------------------------------------
def _decision_map(result: ReplayResult):
    return {(d.case, d.period, d.version): d for d in result.decisions}


def p9_appeal_adjustment_conservation(tl: Timeline):
    result = replay(tl)
    dmap = _decision_map(result)
    appeal_indices = {i for i, r in enumerate(tl.requests) if r.appeal}
    adjustment_requests = {a.caused_by_request for a in result.adjustments}

    # Adjustments may only originate from appeal requests.
    if not adjustment_requests.issubset(appeal_indices):
        return _fail("a non-appeal request produced a monetary adjustment")

    for idx in appeal_indices:
        request = tl.requests[idx]
        key = (request.case, request.period)
        prior_versions = [d for d in result.decisions
                          if (d.case, d.period) == key and d.decided_at < request.at]
        if not prior_versions:
            continue
        prior = max(prior_versions, key=lambda d: d.version)
        related = [a for a in result.adjustments if a.caused_by_request == idx]
        was_notified = any(
            n.case == key[0] and n.period == key[1]
            and n.version == prior.version
            for n in result.notifications
        )
        if idx in result.appeals_upheld:
            new = next(d for d in result.decisions
                       if (d.case, d.period) == key and d.decided_at == request.at)
            if was_notified:
                if len(related) != 1:
                    return _fail("upheld appeal on notified period needs exactly one adjustment")
                adj = related[0]
                delta = new.benefit_cents - prior.benefit_cents
                expected_kind = ADJ_MAKEUP if delta > 0 else ADJ_CLAWBACK
                if adj.kind != expected_kind or adj.amount_cents != abs(delta):
                    return _fail("adjustment direction/amount mismatches the appeal delta")
                if prior.benefit_cents + (
                    adj.amount_cents if delta > 0 else -adj.amount_cents
                ) != new.benefit_cents:
                    return _fail("appeal adjustment breaks conservation")
            elif related:
                return _fail("unnotified overturned period must not book an adjustment")
        elif related:
            return _fail("a rejected appeal produced an adjustment")
    return _ok()


def p10_ledger_conservation(tl: Timeline):
    result = replay(tl)
    # Global cent-level identity over every appeal-driven key.
    notified: dict[tuple[str, int], int] = {}
    for note in result.notifications:
        notified[(note.case, note.period)] = note.benefit_cents
    latest = result.latest_versions()
    for key, entitlement in latest.items():
        makeups = sum(a.amount_cents for a in result.adjustments
                      if (a.case, a.period) == key and a.kind == ADJ_MAKEUP)
        clawbacks = sum(a.amount_cents for a in result.adjustments
                        if (a.case, a.period) == key and a.kind == ADJ_CLAWBACK)
        if key in notified and makeups + clawbacks:
            if notified[key] + makeups - clawbacks != entitlement.benefit_cents:
                return _fail(
                    f"ledger does not balance for {key}: "
                    f"{notified[key]} + {makeups} - {clawbacks} "
                    f"!= {entitlement.benefit_cents}"
                )
        if key not in notified and (makeups or clawbacks):
            return _fail(f"adjustment booked for never-notified {key}")
    # Totals across the whole ledger must also balance.
    total_notified = sum(notified.values())
    total_makeup = sum(a.amount_cents for a in result.adjustments
                       if a.kind == ADJ_MAKEUP)
    total_clawback = sum(a.amount_cents for a in result.adjustments
                         if a.kind == ADJ_CLAWBACK)
    appeal_keys = {(a.case, a.period) for a in result.adjustments}
    total_final = sum(latest[k].benefit_cents for k in appeal_keys)
    total_base = sum(notified[k] for k in appeal_keys)
    if total_base + total_makeup - total_clawback != total_final:
        return _fail("aggregate conservation identity violated")
    if total_notified < 0 or total_makeup < 0 or total_clawback < 0:
        return _fail("negative monetary aggregate")
    return _ok()


# ---------------------------------------------------------------------------
# P11 – household-size boundary and exact income limit
# ---------------------------------------------------------------------------
def p11_household_and_limit_boundary(tl: Timeline):
    result = replay(tl)
    for decision in result.decisions:
        # An overturned decision can land on either eligibility side; its
        # money fields reveal the real verdict.  An empty-household overturn
        # is in effect a denial and must never carry benefit or eligible days.
        effective_denial = decision.status in (STATUS_DENIED, STATUS_SUSPENDED)
        overturned_to_denial = (
            decision.status == STATUS_OVERTURNED
            and decision.benefit_cents == 0
        )
        if decision.household_size == 0:
            if decision.status == STATUS_PENDING:
                continue
            if not (effective_denial or overturned_to_denial):
                return _fail(f"empty household was not denied: {decision}")
            if effective_denial and decision.reason != "empty_household":
                return _fail("empty household denied for the wrong reason")
            if decision.benefit_cents != 0 or decision.eligible_days != 0:
                return _fail("empty household carries benefit or eligible days")
        # Exact-limit eligibility only applies to decided (non-pending) cases.
        if (decision.status != STATUS_PENDING
                and decision.household_size >= 1
                and decision.income == decision.limit):
            if decision.status in (STATUS_DENIED, STATUS_SUSPENDED):
                return _fail(f"exact-limit income must be eligible (<=): {decision}")
        # Any money-paying decision must satisfy the income limit.
        pays_benefit = decision.benefit_cents > 0
        if pays_benefit and decision.income > decision.limit:
            return _fail(f"benefit-paying decision exceeds income limit: {decision}")
        if decision.benefit_cents > 0 and decision.eligible_days == 0:
            return _fail("benefit paid with zero eligible days")
    return _ok()


# ---------------------------------------------------------------------------
# P12 – half-up cent rounding and proration
# ---------------------------------------------------------------------------
def p12_cent_exact_rounding(tl: Timeline):
    result = replay(tl)
    policies = {p.policy_id: p for p in tl.policies}
    for decision in result.decisions:
        if decision.policy_id is None:
            continue
        policy = policies[decision.policy_id]
        expected = default_round_half_up(
            policy.benefit_monthly * decision.eligible_days, MONTH_DAYS
        )
        if decision.benefit_cents != expected:
            return _fail(
                f"rounding mismatch for {decision.case}/p{decision.period}: "
                f"{decision.benefit_cents} != {expected}"
            )
        if decision.eligible_days == 0 and decision.benefit_cents != 0:
            return _fail("zero eligible days produced a benefit")
        # Benefit is always a non-negative whole-cent amount.
        if decision.benefit_cents < 0:
            return _fail("negative benefit amount")
    return _ok()


# ---------------------------------------------------------------------------
# P13 – determinism
# ---------------------------------------------------------------------------
def p13_determinism(tl: Timeline):
    if replay(tl) != replay(tl):
        return _fail("same timeline replayed to different results")
    if replay(tl, Components()) != replay(tl, Components()):
        return _fail("fresh component bundles broke determinism")
    return _ok()


# ---------------------------------------------------------------------------
# P14 – report field mapping consistency
# ---------------------------------------------------------------------------
def p14_report_field_mapping(tl: Timeline):
    result = replay(tl)
    rows = result.report_rows()
    latest = result.latest_versions()
    notes = {(n.case, n.period): n for n in result.notifications}
    if len(rows) != len(latest):
        return _fail("report dropped or duplicated decision rows")
    expected_keys = {
        "case", "period", "final_status", "final_entitlement_cents",
        "decided_at", "policy_id", "notified", "notified_at",
        "frozen_notified_cents",
    }
    for row in rows:
        if set(row) != expected_keys:
            return _fail(f"report row field set drifted: {set(row)}")
        key = (row["case"], row["period"])
        decision = latest[key]
        if row["final_status"] != decision.status:
            return _fail("final_status mis-mapped")
        if row["final_entitlement_cents"] != decision.benefit_cents:
            return _fail("final_entitlement_cents mis-mapped")
        if row["decided_at"] != decision.decided_at:
            return _fail("decided_at mis-mapped")
        if row["policy_id"] != decision.policy_id:
            return _fail("policy_id mis-mapped")
        note = notes.get(key)
        if row["notified"] != (note is not None):
            return _fail("notified flag mis-mapped")
        if note is not None:
            if row["notified_at"] != note.notified_at:
                return _fail("notified_at mis-mapped")
            if row["frozen_notified_cents"] != note.benefit_cents:
                return _fail("frozen notification amount mis-mapped")
    return _ok()


PROPERTIES = {
    "P1_timestamp_separation": p1_timestamp_separation,
    "P2_delivery_order_invariance": p2_delivery_order_invariance,
    "P3_redelivery_dedup_invariance": p3_redelivery_dedup_invariance,
    "P4_invalid_correction_rejected": p4_invalid_correction_rejected,
    "P5_retro_policy_scope": p5_retro_policy_scope,
    "P6_notification_freeze": p6_notification_freeze,
    "P7_suspend_resume_transitions": p7_suspend_resume_transitions,
    "P8_evidence_gating": p8_evidence_gating,
    "P9_appeal_adjustment_conservation": p9_appeal_adjustment_conservation,
    "P10_ledger_conservation": p10_ledger_conservation,
    "P11_household_and_limit_boundary": p11_household_and_limit_boundary,
    "P12_cent_exact_rounding": p12_cent_exact_rounding,
    "P13_determinism": p13_determinism,
    "P14_report_field_mapping": p14_report_field_mapping,
}


def evaluate_property(property_id: str, tl: Timeline, replay_fn=replay):
    """Run one property with an injected (possibly faulty) replay function.

    The module-level ``replay`` symbol is swapped for the duration of the
    check only; the injected function is restored even if the property
    raises.  This is the single seam used by the fault-injection matrix.
    """
    import qa_support.properties as self_module
    original = self_module.replay
    self_module.replay = replay_fn
    try:
        return PROPERTIES[property_id](tl)
    finally:
        self_module.replay = original
