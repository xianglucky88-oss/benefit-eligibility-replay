# Benefit Eligibility Replay

Replay public-benefit eligibility decisions across household and policy versions.

## Modules

- `src/benefit_eligibility_replay/models.py`: immutable result contract.
- `src/benefit_eligibility_replay/parser.py`: input validation and record extraction.
- `src/benefit_eligibility_replay/engine.py`: domain calculation only.
- `src/benefit_eligibility_replay/report.py`: stable output envelope.
- `src/benefit_eligibility_replay/pipeline.py`: orchestration boundary.
- `src/benefit_eligibility_replay/cli.py`: deterministic JSON CLI (injectable `argv`).

## Run

```bash
PYTHONPATH=src python -m benefit_eligibility_replay.cli
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network or credentials are required.

## Reproducible release / verification / audit chain

The offline, standard-library-only release chain lives in [`release/`](release/README.md).
Every entry point runs under `python3 -I` (isolated mode) so a planted
`json.py` in the working or input directory cannot hijack imports, and every
subprocess receives a fixed minimal environment.

```bash
python3 -I release/releng.py verify     # 9-check matrix: default fixture, explicit
                                        # input, module import, sorted JSON,
                                        # determinism, time zones, tests, hashes
python3 -I release/releng.py build      # stage -> verify -> atomic promote;
                                        # failures are moved to dist/quarantine
python3 -I release/releng.py rollback   # point dist/current at a prior release
python3 -I release/releng.py audit --tree <release-dir>   # offline re-verification
bash release/selftest.sh                # full end-to-end self-test in a temp copy
```

A release directory is self-contained: it carries the source, fixtures,
tooling, a synthetic explicit input, `REBUILD.md` and a canonical
`manifest.json` with source/output SHA-256 hashes, interpreter facts, hashed
environment variables and the test summary. An auditor can rebuild and check
the eligibility results on a different machine without any network access.
