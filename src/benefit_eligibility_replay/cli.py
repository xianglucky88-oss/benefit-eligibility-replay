import argparse
import json
from pathlib import Path

from .pipeline import run

DEFAULT_FIXTURE = Path(__file__).parents[2] / "fixtures" / "input.json"


def main(argv: list[str] | None = None) -> None:
    """Run the deterministic CLI.

    ``argv`` is injectable so callers (tests, the release runners) do not have
    to mutate the process-global ``sys.argv``. When omitted, argparse reads
    ``sys.argv[1:]`` exactly as a normal command line would.
    """
    parser = argparse.ArgumentParser(
        description="Replay public-benefit eligibility decisions across household and policy versions."
    )
    parser.add_argument("input", nargs="?", default=str(DEFAULT_FIXTURE))
    # Pass argv=[] explicitly to select the default fixture; omitting argv
    # parses the real command line (sys.argv[1:]).
    args = parser.parse_args(argv)
    payload = json.loads(Path(args.input).read_text())
    print(json.dumps(run(payload), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
