"""Property-based tests over the timeline reference oracle.

The runner is deterministic (fixed base seed); each property is exercised
across handcrafted edge cases and every generator scenario mode.  Assertions
are strict and are never relaxed to make a run green – failures surface as
ordinary ``unittest`` failures with the property id and diagnostic detail.
"""
import qa_support.paths  # noqa: F401

import unittest

from qa_support.properties import PROPERTIES, PROPERTY_IDS
from qa_support.runner import format_report, run_properties

ITERATIONS = 60
BASE_SEED = 20261116


class PropertySuiteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_properties(
            PROPERTIES, iterations=ITERATIONS, base_seed=BASE_SEED,
            do_shrink=True,
        )

    def test_all_properties_hold_across_all_scenario_modes(self):
        if not self.report.ok:
            self.fail("\n" + format_report(self.report))

    def test_every_named_property_was_actually_exercised(self):
        self.assertEqual(
            sorted(self.report.passed), sorted(PROPERTY_IDS)
        )

    def test_iteration_count_is_the_contracted_one(self):
        # Guards against someone weakening coverage by turning the dial down.
        self.assertEqual(self.report.iterations, ITERATIONS)
        self.assertGreaterEqual(ITERATIONS, 50)


class SeedReproducibilityTest(unittest.TestCase):
    def test_same_seed_reproduces_identical_timeline_and_result(self):
        from qa_support.generators import random_timeline
        from qa_support.timeline import replay

        first = random_timeline(BASE_SEED + 4, "retro")
        second = random_timeline(BASE_SEED + 4, "retro")
        self.assertEqual(first, second)
        self.assertEqual(replay(first), replay(second))


if __name__ == "__main__":
    unittest.main()
