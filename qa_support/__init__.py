"""Independent quality-assurance layer for benefit eligibility replay.

This package is deliberately separate from ``src/benefit_eligibility_replay``:

* :mod:`qa_support.timeline` is a self-contained timeline reference model
  (an independent oracle) that keeps application time, evidence effective
  time, decision time and notification time as distinct concepts.
* :mod:`qa_support.generators` produces deterministic random timelines.
* :mod:`qa_support.properties` encodes the invariant / property checks.
* :mod:`qa_support.shrinker` reduces failing timelines to minimal counter
  examples.
* :mod:`qa_support.runner` is a tiny deterministic property-test runner.
* :mod:`qa_support.fault_injection` defines one-fault mutants and the
  localisation matrix that proves each fault class is caught and located.

Nothing in this package imports or modifies the production eligibility
algorithm; the cross-layer tests in ``qa_tests/test_cross_layer.py`` treat
``parser`` / ``engine`` / ``pipeline`` / ``report`` strictly as systems
under test with fixed contracts.
"""
