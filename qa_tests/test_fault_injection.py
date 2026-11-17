"""Counter-example shrinking and fault-localisation tests."""
import qa_support.paths  # noqa: F401

import unittest

from qa_support.fault_injection import (
    FAULT_DEDUP,
    FAULT_POLICY_SELECTION,
    FAULT_REPORT_MAPPING,
    FAULT_TIME_COMPARISON,
    ORACLE_MUTANTS,
    evaluate_oracle_mutants,
)
from qa_support.generators import handcrafted_timelines, random_timeline
from qa_support.properties import PROPERTY_IDS, evaluate_property
from qa_support.runner import MODES
from qa_support.shrinker import is_minimal, shrink, timeline_size


def _timelines():
    return (handcrafted_timelines()
            + [random_timeline(20261116 + i, MODES[i % len(MODES)])
               for i in range(90)])


class FaultInjectionMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verdicts = evaluate_oracle_mutants(
            None, _timelines(), list(PROPERTY_IDS)
        )

    def test_every_mutant_is_killed(self):
        survivors = [m for m, v in self.verdicts.items() if not v["killed"]]
        self.assertEqual(survivors, [])

    def test_every_mutant_is_localised_to_its_named_property(self):
        misplaced = {
            m: (v["expected"], v["failing_properties"])
            for m, v in self.verdicts.items() if not v["localised"]
        }
        self.assertEqual(misplaced, {})

    def test_required_fault_classes_are_covered(self):
        classes = {m.fault_class for m in ORACLE_MUTANTS}
        for required in (
            FAULT_TIME_COMPARISON,
            FAULT_DEDUP,
            FAULT_POLICY_SELECTION,
            FAULT_REPORT_MAPPING,
        ):
            self.assertIn(required, classes)

    def test_fault_targets_are_distinct_components(self):
        targets = [m.target for m in ORACLE_MUTANTS]
        self.assertEqual(len(targets), len(set(targets)))


def _failing_timeline(mutant, property_id, max_seed=40):
    """Find a deterministic input on which the mutant breaks its property.

    Handcrafted edge-case timelines are searched first (they cover faults
    such as rounding that only manifest on partial-month periods), followed
    by seeded random timelines across every scenario mode.
    """
    from qa_support.properties import evaluate_property
    pool = handcrafted_timelines() + [
        random_timeline(20261116 + seed, MODES[seed % len(MODES)])
        for seed in range(max_seed)
    ]
    for tl in pool:
        ok, _ = evaluate_property(property_id, tl, mutant.runner)
        if not ok:
            return tl
    raise AssertionError(
        f"{mutant.mutant_id} never broke {property_id} across the input pool"
    )


class ShrinkerTest(unittest.TestCase):
    def test_shrunk_counterexample_is_smaller_and_still_red(self):
        mutant = next(m for m in ORACLE_MUTANTS
                      if m.fault_class == FAULT_TIME_COMPARISON)
        tl = _failing_timeline(mutant, mutant.primary_property)
        check = lambda t: evaluate_property(  # noqa: E731
            mutant.primary_property, t, mutant.runner
        )
        shrunk = shrink(tl, check)
        self.assertLess(timeline_size(shrunk), timeline_size(tl))
        ok, detail = check(shrunk)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_shrunk_case_is_atom_minimal(self):
        mutant = next(m for m in ORACLE_MUTANTS
                      if m.fault_class == FAULT_DEDUP)
        tl = _failing_timeline(mutant, mutant.primary_property)
        check = lambda t: evaluate_property(  # noqa: E731
            mutant.primary_property, t, mutant.runner
        )
        shrunk = shrink(tl, check)
        self.assertFalse(check(shrunk)[0])
        self.assertTrue(is_minimal(shrunk, check))

    def test_report_mapping_mutant_shrinks_to_a_single_row(self):
        mutant = next(m for m in ORACLE_MUTANTS
                      if m.fault_class == FAULT_REPORT_MAPPING)
        tl = random_timeline(20261122, "general")
        check = lambda t: evaluate_property(  # noqa: E731
            mutant.primary_property, t, mutant.runner
        )
        self.assertFalse(check(tl)[0])
        shrunk = shrink(tl, check)
        self.assertFalse(check(shrunk)[0])
        # The reduced report fault needs only one decided period to expose.
        self.assertEqual(len(shrunk.requests), 1)
        self.assertTrue(is_minimal(shrunk, check))


if __name__ == "__main__":
    unittest.main()
