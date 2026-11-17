# Benefit Eligibility Replay

Replay public-benefit eligibility decisions across household and policy versions.

## Modules

- `src/benefit_eligibility_replay/models.py`: immutable result contract.
- `src/benefit_eligibility_replay/parser.py`: input validation and record extraction.
- `src/benefit_eligibility_replay/engine.py`: domain calculation only.
- `src/benefit_eligibility_replay/report.py`: stable output envelope.
- `src/benefit_eligibility_replay/pipeline.py`: orchestration boundary.
- `src/benefit_eligibility_replay/cli.py`: deterministic JSON CLI.

## Run

```bash
PYTHONPATH=src python -m benefit_eligibility_replay.cli
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network or credentials are required.

## Independent quality-assurance layer

The production algorithm is deliberately small and has **no effective-time
semantics yet**.  Rather than rewriting it, the repository ships a separate,
independent QA layer that (a) defines what correct time-aware replay must
look like, (b) proves those properties exhaustively, and (c) pinpoints where
a regression would live.  Nothing under `qa_support/` or `qa_tests/` imports
the eligibility algorithm into its oracle, and the production code is never
modified to make tests pass.

### Timeline reference model (`qa_support/timeline.py`)

An event-sourced oracle that keeps four time concepts strictly distinct:

| Concept | Field | Meaning |
| --- | --- | --- |
| application time | application event `at` | when the household applied |
| evidence effective time | fact/event `at` | when a fact becomes legally in force |
| decision time | `DecisionRequest.at` | when the agency (re)decides a period |
| notification time | `DecisionRequest.notify_at` | when the decision is dispatched; frozen thereafter |

Encoded rules: natural-key event de-duplication, rejection of invalid
corrections, effective-time gating (a correction effective after the
decision cannot influence it), policy selection by effective range **and**
enactment time (retroactivity only reaches in-scope, not-yet-notified
decisions), evidence gating, an explicit suspend/resume state machine,
half-up cent rounding with proration, appeal overturns, and the conservation
identity `notified + makeup − clawback = final entitlement`.

### Property tests (`qa_support/properties.py`)

Fourteen invariant properties, P1–P14, run over deterministic seeded
generators (`qa_support/generators.py`) and handcrafted edge cases via a
small stdlib runner (`qa_support/runner.py`): time separation, delivery-order
invariance, redelivery de-duplication, invalid-correction rejection,
retroactive policy scope, notification freezing, suspend/resume, evidence
gating, appeal adjustment conservation, ledger conservation, household/limit
boundaries, cent-exact rounding, determinism and report field mapping.

### Fault injection and localisation (`qa_support/fault_injection.py`)

Seven one-fault oracle mutants (time comparison, event de-duplication,
policy selection, rounding, evidence, notification freeze, report mapping)
and four production-module mutants.  Each mutant is proven to be **killed**
and **localised** – its named primary property fails, so a red build points
at the faulty component and time semantic instead of a generic breakage.

### Minimal counter-example shrinking (`qa_support/shrinker.py`)

Failing timelines are reduced with delta debugging plus a structured scalar
sweep to an atom-minimal counter-example; the suite asserts minimality.

### Running the QA layer

```bash
# stdlib only, isolated mode safe, no PYTHONPATH needed
python3 run_qa.py
# or
python3 qa_tests/run_all.py
# property report on its own
PYTHONPATH=src:. python3 -m unittest qa_tests.test_properties -v
```

### Known, documented gap

`engine.calculate` currently applies the last change regardless of its
effective time and never reads the `at` fields.  `DocumentedTimeSemanticsGapTest`
pins this behaviour honestly rather than weakening an assertion.  When time
semantics are added to the engine, those tests fail by design and should be
replaced by direct equivalence tests against `qa_support.timeline`.
