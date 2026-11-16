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
