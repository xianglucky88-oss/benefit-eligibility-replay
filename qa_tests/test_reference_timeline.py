"""Tests for the independent timeline reference model itself.

These pin the regulatory semantics with explicit hand-built scenarios so the
oracle cannot silently drift: four distinct time concepts, late/effective
corrections, retroactive enactment, notification freezing, suspend/resume,
appeal adjustments and cent-exact money conservation.
"""
import qa_support.paths  # noqa: F401  (import-path bootstrap)

import unittest

from qa_support.generators import DAY0, handcrafted_timelines, random_timeline
from qa_support.timeline import (
    ADJ_CLAWBACK,
    ADJ_MAKEUP,
    Components,
    DecisionRequest,
    Event,
    Policy,
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_OVERTURNED,
    STATUS_PENDING,
    STATUS_RESUMED,
    STATUS_SUSPENDED,
    Timeline,
    default_round_half_up,
    replay,
    EVENT_APPLICATION,
    EVENT_EVIDENCE,
    EVENT_HOUSEHOLD,
    EVENT_INCOME,
)


def _policy(pid="P", **over):
    params = dict(effective_from=0, effective_to=None, enacted_at=0,
                  income_limit_per_person=1000, benefit_monthly=600)
    params.update(over)
    return Policy(pid, **params)


def _base_events(case="C", members=1, income=500, app_at=DAY0):
    return (
        Event("A", case, EVENT_APPLICATION, app_at, {}),
        Event("H", case, EVENT_HOUSEHOLD, app_at, {"members": members}),
        Event("I", case, EVENT_INCOME, app_at, {"amount_cents": income}),
        Event("D", case, EVENT_EVIDENCE, app_at, {"doc": "id"}),
    )


def _request(case="C", at=DAY0 + 1, notify_at=DAY0 + 2,
             start=DAY0, end=DAY0 + 29, period=0):
    return DecisionRequest(case, period, start, end, at, notify_at)


class ReferenceModelSemanticsTest(unittest.TestCase):
    def test_handcrafted_scenarios_match_documented_semantics(self):
        results = [replay(tl) for tl in handcrafted_timelines()]

        # Empty household -> denied with the structural reason.
        self.assertEqual(results[0].decisions[0].status, STATUS_DENIED)
        self.assertEqual(results[0].decisions[0].reason, "empty_household")

        # Exact-limit income -> eligible, full-month benefit unchanged.
        exact = results[1].decisions[0]
        self.assertEqual(exact.status, STATUS_APPROVED)
        self.assertEqual(exact.income, exact.limit)
        self.assertEqual(exact.benefit_cents, 600)

        # Late income correction (effective after decision) is ignored.
        late = results[2].decisions[0]
        self.assertEqual(late.income, 900)
        self.assertEqual(late.policy_id, "P-base")

        # Re-decision after notification leaves the notification frozen.
        frozen = results[3]
        self.assertEqual(len(frozen.notifications), 1)
        self.assertEqual(frozen.notifications[0].benefit_cents, 600)
        self.assertEqual(len(frozen.decisions), 2)

    def test_application_vs_evidence_effective_time_are_distinct(self):
        # Application arrives on day 5; a correction effective day 3 (before
        # the decision on day 6) must count, one effective day 7 must not.
        events = _base_events(app_at=DAY0 + 5)
        events += (
            Event("I-early", "C", EVENT_INCOME, DAY0 + 3,
                  {"amount_cents": 200}),
            Event("I-late", "C", EVENT_INCOME, DAY0 + 7,
                  {"amount_cents": 5000}),
        )
        request = _request(at=DAY0 + 6, notify_at=DAY0 + 6,
                           start=DAY0, end=DAY0 + 29)
        result = replay(Timeline((), events, (_policy(),), (request,)))
        decision = result.decisions[0]
        self.assertEqual(decision.income, 700)       # 500 + 200, not +5000
        self.assertEqual(decision.status, STATUS_APPROVED)

    def test_decision_time_vs_notification_time_are_independent(self):
        events = _base_events()
        request = _request(at=DAY0 + 1, notify_at=DAY0 + 9)
        first = replay(Timeline((), events, (_policy(),), (request,)))
        later = replay(Timeline(
            (), events, (_policy(),),
            (DecisionRequest("C", 0, DAY0, DAY0 + 29, DAY0 + 1, DAY0 + 99),),
        ))
        # Decisions identical even though notification moved much later.
        self.assertEqual(first.decisions, later.decisions)
        self.assertEqual(first.notifications[0].benefit_cents,
                         later.notifications[0].benefit_cents)
        self.assertNotEqual(first.notifications[0].notified_at,
                            later.notifications[0].notified_at)

    def test_retroactive_policy_scope_and_enactment_boundary(self):
        base = _policy("P-base", income_limit_per_person=1000,
                       benefit_monthly=900)
        retro = _policy("P-retro", effective_from=DAY0,
                        enacted_at=DAY0 + 10, income_limit_per_person=400,
                        benefit_monthly=600)
        events = _base_events(income=500)
        # Decided one day before enactment -> base policy only.
        before = replay(Timeline(
            (), events, (base, retro),
            (_request(at=DAY0 + 9, notify_at=None),),
        ))
        self.assertEqual(before.decisions[0].policy_id, "P-base")
        # Decided on enactment day -> retroactive policy is selectable.
        on_day = replay(Timeline(
            (), events, (base, retro),
            (_request(at=DAY0 + 10, notify_at=None),),
        ))
        self.assertEqual(on_day.decisions[0].policy_id, "P-retro")
        # Outside the retro effective window -> base policy again.
        retro_scoped = _policy("P-retro", effective_from=DAY0 + 40,
                               enacted_at=DAY0 + 10)
        outside = replay(Timeline(
            (), events, (base, retro_scoped),
            (_request(at=DAY0 + 10, notify_at=None),),
        ))
        self.assertEqual(outside.decisions[0].policy_id, "P-base")

    def test_notification_freeze_under_later_recomputation(self):
        events = _base_events() + (
            Event("I-late", "C", EVENT_INCOME, DAY0 + 40,
                  {"amount_cents": 9000}),
        )
        requests = (
            _request(at=DAY0 + 1, notify_at=DAY0 + 2),
            DecisionRequest("C", 0, DAY0, DAY0 + 29, DAY0 + 45, None),
        )
        result = replay(Timeline((), events, (_policy(),), requests))
        note = result.notifications[0]
        self.assertEqual(note.benefit_cents, 600)
        # The second version is denied; the notification snapshot is frozen.
        self.assertEqual(result.decisions[-1].status, STATUS_SUSPENDED)
        prefix = replay(Timeline((), events, (_policy(),), requests)
                        .cutoff(note.notified_at))
        self.assertEqual(prefix.notifications[0], note)

    def test_suspend_then_resume_state_machine(self):
        # Decisions at days 1, 25, 45.  Income is an additive stream, so the
        # recovery correction zeroes the disqualifying amount before a fresh
        # qualifying income becomes effective – all before the decision that
        # must observe the change.
        events = _base_events(income=500) + (
            Event("I-up", "C", EVENT_INCOME, DAY0 + 10,
                  {"amount_cents": 5000}),
            Event("I-up-off", "C", EVENT_INCOME, DAY0 + 28,
                  {"amount_cents": -5000}),
            Event("I-down", "C", EVENT_INCOME, DAY0 + 30,
                  {"amount_cents": 100}),
        )
        requests = (
            DecisionRequest("C", 0, DAY0, DAY0 + 29, DAY0 + 1, DAY0 + 2),
            DecisionRequest("C", 0, DAY0, DAY0 + 29, DAY0 + 25, None),
            DecisionRequest("C", 0, DAY0, DAY0 + 29, DAY0 + 45, None),
        )
        result = replay(Timeline((), events, (_policy(),), requests))
        statuses = [d.status for d in result.decisions]
        self.assertEqual(statuses,
                         [STATUS_APPROVED, STATUS_SUSPENDED, STATUS_RESUMED])

    def test_appeal_overturn_books_conservation_adjustment(self):
        # A partial-month approval is notified (frozen at a prorated amount).
        # A suspension is decided but not notified; the appeal restores full
        # eligibility, so a makeup reconciles the frozen notification up to
        # the full-month entitlement while the notification stays frozen.
        policy = _policy("P", benefit_monthly=600)
        events = (
            Event("A", "C", EVENT_APPLICATION, DAY0, {}),
            Event("H", "C", EVENT_HOUSEHOLD, DAY0, {"members": 1}),
            Event("I", "C", EVENT_INCOME, DAY0, {"amount_cents": 500}),
            Event("D", "C", EVENT_EVIDENCE, DAY0, {"doc": "id"}),
            Event("I-up", "C", EVENT_INCOME, DAY0 + 10,
                  {"amount_cents": 5000}),
            Event("I-up-off", "C", EVENT_INCOME, DAY0 + 28,
                  {"amount_cents": -5000}),
            Event("I-down", "C", EVENT_INCOME, DAY0 + 30,
                  {"amount_cents": 100}),
        )
        requests = (
            # First decision covers only 15 eligible days -> 300 cents.
            DecisionRequest("C", 0, DAY0, DAY0 + 14, DAY0 + 1, DAY0 + 2),
            DecisionRequest("C", 0, DAY0, DAY0 + 14, DAY0 + 25, None),
            DecisionRequest("C", 0, DAY0, DAY0 + 29, DAY0 + 50, DAY0 + 51,
                            appeal=True),
        )
        result = replay(Timeline((), events, (policy,), requests))
        self.assertEqual(result.decisions[-1].status, STATUS_OVERTURNED)
        self.assertEqual(result.appeals_upheld, (2,))
        self.assertEqual(len(result.adjustments), 1)
        adj = result.adjustments[0]
        self.assertEqual(adj.kind, ADJ_MAKEUP)
        notified = result.notifications[0]
        final = result.latest_versions()[("C", 0)]
        self.assertGreater(final.benefit_cents, notified.benefit_cents)
        self.assertEqual(
            notified.benefit_cents + adj.amount_cents, final.benefit_cents
        )
        # The notification snapshot itself is unchanged (frozen).
        self.assertEqual(result.notifications[0].benefit_cents,
                         notified.benefit_cents)

    def test_clawback_on_appeal_overturning_an_approval(self):
        # The first decision approves on the facts available at day 1.  A
        # household correction effective before the appeal (but after the
        # original decision) reveals zero members – a structural denial;
        # the frozen notification is compensated via a clawback rather than
        # rewritten.  Distinct effective times keep this a genuine
        # correction rather than a redelivery (which would be de-duplicated).
        generous = _policy("P-g", benefit_monthly=900)
        events = (
            Event("A", "C", EVENT_APPLICATION, DAY0, {}),
            Event("H", "C", EVENT_HOUSEHOLD, DAY0, {"members": 1}),
            Event("I", "C", EVENT_INCOME, DAY0, {"amount_cents": 100}),
            Event("D", "C", EVENT_EVIDENCE, DAY0, {"doc": "id"}),
            Event("H-fix", "C", EVENT_HOUSEHOLD, DAY0 + 10,
                  {"members": 0}),
        )
        requests = (
            DecisionRequest("C", 0, DAY0, DAY0 + 29, DAY0 + 1, DAY0 + 2),
            DecisionRequest("C", 0, DAY0, DAY0 + 29, DAY0 + 20, DAY0 + 21,
                            appeal=True),
        )
        result = replay(Timeline((), events, (generous,), requests))
        self.assertEqual(result.appeals_upheld, (1,))
        self.assertEqual(len(result.adjustments), 1)
        adj = result.adjustments[0]
        self.assertEqual(adj.kind, ADJ_CLAWBACK)
        notified = result.notifications[0].benefit_cents
        final = result.latest_versions()[("C", 0)].benefit_cents
        self.assertEqual(notified - adj.amount_cents, final)
        self.assertEqual(final, 0)
        # The original notification remains frozen at the approved amount.
        self.assertEqual(result.notifications[0].benefit_cents, 900)

    def test_missing_application_evidence_is_pending(self):
        events = (
            Event("H", "C", EVENT_HOUSEHOLD, DAY0, {"members": 1}),
            Event("I", "C", EVENT_INCOME, DAY0, {"amount_cents": 100}),
        )
        result = replay(Timeline(
            (), events, (_policy(),), (_request(notify_at=None),)
        ))
        self.assertEqual(result.decisions[0].status, STATUS_PENDING)
        self.assertEqual(result.decisions[0].benefit_cents, 0)

    def test_invalid_correction_rejected_and_duplicates_ignored(self):
        events = list(_base_events())
        original_len = len(events)
        # Exact redelivery.
        events.append(Event("A-dup", "C", EVENT_APPLICATION, DAY0, {},
                            delivered_at=DAY0 + 3))
        # Malformed correction.
        events.append(Event("I-bad", "C", EVENT_INCOME, DAY0 + 2,
                            {"amount_cents": 999999}, valid=False))
        result = replay(Timeline((), tuple(events), (_policy(),),
                                 (_request(notify_at=None),)))
        self.assertEqual(len(result.duplicate_events), 1)
        self.assertEqual(len(result.rejected_events), 1)
        self.assertEqual(result.decisions[0].income, 500)
        self.assertEqual(original_len, 4)

    def test_rounding_is_half_up_to_whole_cents(self):
        self.assertEqual(default_round_half_up(9015, 30), 301)  # 300.5 up
        self.assertEqual(default_round_half_up(600, 30), 20)
        self.assertEqual(default_round_half_up(0, 30), 0)

    def test_partial_month_proration_through_full_replay(self):
        from qa_support.generators import handcrafted_timelines
        # Handcrafted scenario 7 carries the four partial-month requests.
        proration = [tl for tl in handcrafted_timelines()
                     if any(p.policy_id == "P-prorate" for p in tl.policies)][0]
        result = replay(proration)
        by_days = {d.eligible_days: d.benefit_cents for d in result.decisions}
        self.assertEqual(by_days, {15: 301, 10: 200, 7: 140, 1: 20})

    def test_enactment_boundary_handcrafted_pair(self):
        from qa_support.generators import handcrafted_timelines
        on_day, after_day = [
            tl for tl in handcrafted_timelines()
            if any(e.case == "C7" for e in tl.events)
        ]
        self.assertEqual(replay(on_day).decisions[0].policy_id, "P-on")
        self.assertEqual(replay(after_day).decisions[0].policy_id, "P-base")

    def test_corrupt_redelivery_keeps_original_fact(self):
        from qa_support.generators import handcrafted_timelines
        corrupt = [tl for tl in handcrafted_timelines()
                   if any(e.event_id == "I4-dup" for e in tl.events)][0]
        result = replay(corrupt)
        self.assertEqual(result.decisions[0].income, 500)
        self.assertTrue(result.duplicate_events)

    def test_evidence_gating_handcrafted_pair(self):
        from qa_support.generators import handcrafted_timelines
        gated = [tl for tl in handcrafted_timelines()
                 if any(e.case == "C5" for e in tl.events)]
        without_app, with_app = gated
        self.assertEqual(replay(without_app).decisions[0].status,
                         STATUS_PENDING)
        self.assertEqual(replay(with_app).decisions[0].status,
                         STATUS_APPROVED)

    def test_replay_is_deterministic_and_order_invariant(self):
        tl = random_timeline(7, "general")
        self.assertEqual(replay(tl), replay(tl))
        shuffled = tl.with_events(tuple(sorted(
            tl.events, key=lambda e: e.event_id, reverse=True
        )))
        self.assertEqual(replay(shuffled), replay(tl))

    def test_component_bundle_swap_does_not_affect_default_replay(self):
        tl = random_timeline(11, "boundary")
        self.assertEqual(replay(tl), replay(tl, Components()))


if __name__ == "__main__":
    unittest.main()
