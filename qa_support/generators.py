"""Deterministic timeline generators for property-based testing.

A seeded :class:`random.Random` drives every choice so a failing seed is
always reproducible.  Generators deliberately favour boundary-rich values
(zero members, exact-limit income, zero-day and full-month periods, same-day
effective/decision/notification times, retroactive policy enactment) because
those are where time-semantic faults hide.
"""
from __future__ import annotations

import random
from dataclasses import replace

from .timeline import (
    Case,
    DecisionRequest,
    Event,
    Policy,
    Timeline,
    EVENT_APPLICATION,
    EVENT_EVIDENCE,
    EVENT_HOUSEHOLD,
    EVENT_INCOME,
    MONTH_DAYS,
)

DAY0 = 1000  # fixed epoch so generated day ordinals stay small and readable


def _event_id(case, kind, seq):
    return f"E-{case}-{kind}-{seq}"


def random_timeline(seed: int, mode: str = "general") -> Timeline:
    """Generate one timeline.

    Modes:
      * ``general``    – arbitrary multi-case, multi-period timelines;
      * ``boundary``   – exact income limits, empty households, tight days;
      * ``retro``      – policies enacted after their effective start;
      * ``duplicates`` – repeated delivery / invalid corrections;
      * ``appeal``     – deny-then-appeal sequences with adjustments;
      * ``freeze``     – notifications followed by later recomputations.
    """
    rng = random.Random(seed)
    case_count = rng.randint(1, 3)
    cases = tuple(Case(case=f"C{i}", application_at=DAY0) for i in range(case_count))

    policies = _generate_policies(rng, mode)
    events: list[Event] = []
    requests: list[DecisionRequest] = []
    seq = 0

    for case in cases:
        seq = _generate_case_events(rng, mode, case.case, events, seq)
        _generate_requests(rng, mode, case.case, requests, policies)

    if mode in ("duplicates", "general"):
        events = _inject_duplicates(rng, mode, events)
    if mode in ("duplicates", "boundary"):
        events = _inject_invalid_corrections(rng, mode, events)

    rng.shuffle(events)  # delivery order is deliberately arbitrary
    return Timeline(
        cases=cases,
        events=tuple(events),
        policies=tuple(policies),
        requests=tuple(sorted(requests, key=lambda r: (r.at, r.case, r.period))),
    )


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------
def _generate_policies(rng, mode):
    limit = rng.choice((500, 1000, 1200))
    benefit = rng.choice((300, 500, 750))
    base = Policy(
        policy_id="P-base",
        effective_from=DAY0 - 200,
        effective_to=None,
        enacted_at=DAY0 - 200,
        income_limit_per_person=limit,
        benefit_monthly=benefit,
    )
    policies = [base]
    if mode == "retro" or (mode == "general" and rng.random() < 0.4):
        # Retroactive version: effective in the past but enacted later.
        retro_start = DAY0 + rng.randint(1, 2 * MONTH_DAYS)
        enacted = retro_start + rng.randint(10, 40)
        policies.append(Policy(
            policy_id="P-retro",
            effective_from=retro_start,
            effective_to=None,
            enacted_at=enacted,
            income_limit_per_person=limit + rng.choice((-200, 200)),
            benefit_monthly=benefit + rng.choice((-50, 100)),
        ))
    return policies


# ---------------------------------------------------------------------------
# Events per case
# ---------------------------------------------------------------------------
def _generate_case_events(rng, mode, case, events, seq):
    # Application event (application time).
    app_at = DAY0 + rng.randint(0, 5)
    events.append(Event(
        event_id=_event_id(case, EVENT_APPLICATION, seq),
        case=case, kind=EVENT_APPLICATION, at=app_at,
        payload={"periods": 3}, delivered_at=app_at,
    ))
    seq += 1

    # Household size; boundary mode sometimes yields an empty household.
    if mode == "boundary" and rng.random() < 0.3:
        members = 0
    else:
        members = rng.randint(1, 4)
    events.append(Event(
        event_id=_event_id(case, EVENT_HOUSEHOLD, seq),
        case=case, kind=EVENT_HOUSEHOLD, at=app_at,
        payload={"members": members},
    ))
    seq += 1

    # Initial income, often pinned exactly to the limit boundary.
    limit_guess = 1000
    if mode == "boundary":
        income = members * limit_guess + rng.choice((-1, 0, 1))
    else:
        income = rng.choice((
            members * limit_guess,
            members * limit_guess - 50,
            members * limit_guess + 50,
            rng.randint(0, members * limit_guess * 2),
        ))
    events.append(Event(
        event_id=_event_id(case, EVENT_INCOME, seq),
        case=case, kind=EVENT_INCOME, at=app_at,
        payload={"amount_cents": max(0, income)},
    ))
    seq += 1

    # Application evidence document.
    events.append(Event(
        event_id=_event_id(case, EVENT_EVIDENCE, seq),
        case=case, kind=EVENT_EVIDENCE, at=app_at,
        payload={"doc": "id-attachment"},
    ))
    seq += 1

    # Later income / household changes at varied effective times, including
    # some that land after decision time (late corrections).  Distinct
    # corrections never share a (kind, effective-time) slot, so events with
    # equal natural keys are always genuine redeliveries.
    used_slots = {(e.kind, e.at) for e in events if e.case == case}
    change_count = rng.randint(1, 3)
    for _ in range(change_count):
        kind = rng.choice((EVENT_INCOME, EVENT_HOUSEHOLD))
        at = DAY0 + rng.randint(0, 4 * MONTH_DAYS)
        while (kind, at) in used_slots:
            at += 1
        used_slots.add((kind, at))
        if kind == EVENT_INCOME:
            payload = {"amount_cents": rng.randint(0, members * 1500 + 1)}
        else:
            payload = {"members": rng.randint(0, 4)}
        events.append(Event(
            event_id=_event_id(case, kind, seq), case=case, kind=kind,
            at=at, payload=payload,
            delivered_at=at + rng.randint(0, 10),
        ))
        seq += 1

    # A supplementary evidence upload (used by补证据 properties).  Unique
    # effective times keep distinct uploads from collapsing into one dedup
    # key while identical redelivery still counts as a duplicate.
    if rng.random() < 0.6:
        used_days = {e.at for e in events if e.case == case}
        candidate = DAY0 + rng.randint(0, 3 * MONTH_DAYS)
        while candidate in used_days:
            candidate += 1
        events.append(Event(
            event_id=_event_id(case, EVENT_EVIDENCE, seq),
            case=case, kind=EVENT_EVIDENCE, at=candidate,
            payload={"doc": f"supp-{seq}"},
        ))
        seq += 1
    return seq


def _generate_requests(rng, mode, case, requests, policies):
    period_count = rng.randint(1, 3)
    for period in range(period_count):
        start = DAY0 + period * MONTH_DAYS
        end = start + MONTH_DAYS - 1
        decision_at = start + rng.randint(0, 5)
        notify = rng.random() < 0.75
        notify_at = decision_at + rng.randint(0, 5) if notify else None
        requests.append(DecisionRequest(
            case=case, period=period, period_start=start, period_end=end,
            at=decision_at, notify_at=notify_at,
        ))

    if mode == "appeal" or rng.random() < 0.35:
        period = rng.randrange(period_count)
        start = DAY0 + period * MONTH_DAYS
        end = start + MONTH_DAYS - 1
        appeal_at = start + rng.randint(8, 25)
        requests.append(DecisionRequest(
            case=case, period=period, period_start=start, period_end=end,
            at=appeal_at,
            notify_at=appeal_at + rng.randint(1, 5),
            appeal=True,
        ))

    if mode == "freeze" or rng.random() < 0.4:
        # Re-decision of an earlier period after its notification went out.
        period = rng.randrange(period_count)
        start = DAY0 + period * MONTH_DAYS
        end = start + MONTH_DAYS - 1
        requests.append(DecisionRequest(
            case=case, period=period, period_start=start, period_end=end,
            at=start + rng.randint(20, 60),
            notify_at=None,
        ))


def _inject_duplicates(rng, mode, events):
    extra = list(events)
    sample = rng.sample(events, k=min(len(events), rng.randint(1, 3)))
    for event in sample:
        # Same natural key, new event id, possibly different intake time:
        # this models an at-least-once redelivery.
        extra.append(Event(
            event_id=event.event_id + "-redelivered",
            case=event.case, kind=event.kind, at=event.at,
            payload=dict(event.payload),
            delivered_at=(event.delivered_at or event.at) + rng.randint(1, 9),
            valid=event.valid,
        ))
    return extra


def _inject_invalid_corrections(rng, mode, events):
    extra = list(events)
    changeable = [e for e in events if e.kind in (EVENT_INCOME, EVENT_HOUSEHOLD)]
    if changeable:
        event = rng.choice(changeable)
        extra.append(Event(
            event_id=event.event_id + "-invalid",
            case=event.case, kind=event.kind,
            at=event.at + rng.randint(1, 20),
            payload=dict(event.payload), valid=False,
        ))
    return extra


# ---------------------------------------------------------------------------
# Hand-built targeted scenarios
# ---------------------------------------------------------------------------
def handcrafted_timelines():
    """Small explicit scenarios covering edge cases generators may miss."""
    base_policy = Policy(
        policy_id="P-base", effective_from=0, effective_to=None,
        enacted_at=0, income_limit_per_person=1000, benefit_monthly=600,
    )
    out = []

    # 1. Empty household -> always denied regardless of income.
    out.append(Timeline(
        cases=(Case("C0", DAY0),),
        events=(
            Event("A0", "C0", EVENT_APPLICATION, DAY0, {}),
            Event("H0", "C0", EVENT_HOUSEHOLD, DAY0, {"members": 0}),
            Event("I0", "C0", EVENT_INCOME, DAY0, {"amount_cents": 0}),
            Event("D0", "C0", EVENT_EVIDENCE, DAY0, {"doc": "id"}),
        ),
        policies=(base_policy,),
        requests=(DecisionRequest("C0", 0, DAY0, DAY0 + 29, DAY0 + 1, DAY0 + 2),),
    ))

    # 2. Exact-limit income is eligible (<= boundary), full month benefit.
    out.append(Timeline(
        cases=(Case("C1", DAY0),),
        events=(
            Event("A1", "C1", EVENT_APPLICATION, DAY0, {}),
            Event("H1", "C1", EVENT_HOUSEHOLD, DAY0, {"members": 2}),
            Event("I1", "C1", EVENT_INCOME, DAY0, {"amount_cents": 2000}),
            Event("D1", "C1", EVENT_EVIDENCE, DAY0, {"doc": "id"}),
        ),
        policies=(base_policy,),
        requests=(DecisionRequest("C1", 0, DAY0, DAY0 + 29, DAY0 + 1, DAY0 + 2),),
    ))

    # 3. Late income correction (effective after decision) must not alter
    #    the decision; an earlier correction must.
    retro_policy = Policy(
        policy_id="P-retro", effective_from=DAY0, effective_to=None,
        enacted_at=DAY0 + 40, income_limit_per_person=400,
        benefit_monthly=600,
    )
    out.append(Timeline(
        cases=(Case("C2", DAY0),),
        events=(
            Event("A2", "C2", EVENT_APPLICATION, DAY0, {}),
            Event("H2", "C2", EVENT_HOUSEHOLD, DAY0, {"members": 1}),
            Event("I2", "C2", EVENT_INCOME, DAY0, {"amount_cents": 900}),
            Event("D2", "C2", EVENT_EVIDENCE, DAY0, {"doc": "id"}),
            Event("I2b", "C2", EVENT_INCOME, DAY0 + 3, {"amount_cents": 5000}),
        ),
        policies=(base_policy, retro_policy),
        requests=(DecisionRequest("C2", 0, DAY0, DAY0 + 29, DAY0 + 2, DAY0 + 3),),
    ))

    # 4. Notify, then a later re-decision: notification stays frozen.
    out.append(Timeline(
        cases=(Case("C3", DAY0),),
        events=(
            Event("A3", "C3", EVENT_APPLICATION, DAY0, {}),
            Event("H3", "C3", EVENT_HOUSEHOLD, DAY0, {"members": 1}),
            Event("I3", "C3", EVENT_INCOME, DAY0, {"amount_cents": 500}),
            Event("D3", "C3", EVENT_EVIDENCE, DAY0, {"doc": "id"}),
            Event("I3b", "C3", EVENT_INCOME, DAY0 + 40, {"amount_cents": 9000}),
        ),
        policies=(base_policy,),
        requests=(
            DecisionRequest("C3", 0, DAY0, DAY0 + 29, DAY0 + 1, DAY0 + 2),
            DecisionRequest("C3", 0, DAY0, DAY0 + 29, DAY0 + 45, None),
        ),
    ))

    # 5. Corrupt redelivery: an at-least-once channel re-sends an income
    #    event with a mutated payload but the same natural key.  Correct
    #    natural-key dedup keeps the original fact; last-write-wins does not.
    out.append(Timeline(
        cases=(Case("C4", DAY0),),
        events=(
            Event("A4", "C4", EVENT_APPLICATION, DAY0, {}),
            Event("H4", "C4", EVENT_HOUSEHOLD, DAY0, {"members": 2}),
            Event("I4", "C4", EVENT_INCOME, DAY0, {"amount_cents": 500}),
            Event("D4", "C4", EVENT_EVIDENCE, DAY0, {"doc": "id"}),
            Event("I4-dup", "C4", EVENT_INCOME, DAY0,
                  {"amount_cents": 9000}, delivered_at=DAY0 + 2),
        ),
        policies=(base_policy,),
        requests=(DecisionRequest("C4", 0, DAY0, DAY0 + 29, DAY0 + 1, DAY0 + 2),),
    ))

    # 6. Evidence gating: identical facts with and without an application
    #    event, so pending-vs-decided behaviour is directly observable.
    gated_events = (
        Event("H5", "C5", EVENT_HOUSEHOLD, DAY0, {"members": 1}),
        Event("I5", "C5", EVENT_INCOME, DAY0, {"amount_cents": 100}),
        Event("D5", "C5", EVENT_EVIDENCE, DAY0, {"doc": "id"}),
    )
    out.append(Timeline(
        cases=(Case("C5", DAY0),),
        events=gated_events,
        policies=(base_policy,),
        requests=(DecisionRequest("C5", 0, DAY0, DAY0 + 29, DAY0 + 1, None),),
    ))
    out.append(Timeline(
        cases=(Case("C5", DAY0),),
        events=gated_events + (
            Event("A5", "C5", EVENT_APPLICATION, DAY0, {}),
        ),
        policies=(base_policy,),
        requests=(DecisionRequest("C5", 0, DAY0, DAY0 + 29, DAY0 + 1, None),),
    ))

    # 7. Partial-month proration across several eligible-day lengths.
    proration_policy = Policy(
        policy_id="P-prorate", effective_from=0, effective_to=None,
        enacted_at=0, income_limit_per_person=1000, benefit_monthly=601,
    )
    proration_events = (
        Event("A6", "C6", EVENT_APPLICATION, DAY0, {}),
        Event("H6", "C6", EVENT_HOUSEHOLD, DAY0, {"members": 1}),
        Event("I6", "C6", EVENT_INCOME, DAY0, {"amount_cents": 100}),
        Event("D6", "C6", EVENT_EVIDENCE, DAY0, {"doc": "id"}),
    )
    out.append(Timeline(
        cases=(Case("C6", DAY0),),
        events=proration_events,
        policies=(proration_policy,),
        requests=tuple(
            DecisionRequest("C6", period, DAY0, DAY0 + days - 1,
                            DAY0 + 1, None)
            for period, days in ((0, 15), (1, 10), (2, 7), (3, 1))
        ),
    ))

    # 8. Enactment boundary: one policy enacted on the decision day and an
    #    otherwise identical one enacted the day after.
    retro_on_day = Policy(
        policy_id="P-on", effective_from=DAY0, effective_to=None,
        enacted_at=DAY0 + 10, income_limit_per_person=1000,
        benefit_monthly=600,
    )
    enactment_events = (
        Event("A7", "C7", EVENT_APPLICATION, DAY0, {}),
        Event("H7", "C7", EVENT_HOUSEHOLD, DAY0, {"members": 1}),
        Event("I7", "C7", EVENT_INCOME, DAY0, {"amount_cents": 500}),
        Event("D7", "C7", EVENT_EVIDENCE, DAY0, {"doc": "id"}),
    )
    enactment_request = DecisionRequest(
        "C7", 0, DAY0, DAY0 + 29, DAY0 + 10, None
    )
    out.append(Timeline(
        cases=(Case("C7", DAY0),),
        events=enactment_events,
        policies=(base_policy, retro_on_day),
        requests=(enactment_request,),
    ))
    out.append(Timeline(
        cases=(Case("C7", DAY0),),
        events=enactment_events,
        policies=(base_policy, replace(retro_on_day,
                                       policy_id="P-after",
                                       enacted_at=DAY0 + 11)),
        requests=(enactment_request,),
    ))
    return out
