import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import benefit_eligibility_replay
from benefit_eligibility_replay.cli import main as cli_main
from benefit_eligibility_replay.engine import calculate
from benefit_eligibility_replay.parser import load_records
from benefit_eligibility_replay.pipeline import run


def fixture():
    return json.loads(
        (Path(__file__).parents[1] / "fixtures" / "input.json").read_text()
    )


class TestCore(unittest.TestCase):
    def test_pipeline_and_modules(self):
        payload = fixture()
        rows = load_records(payload)
        self.assertTrue(rows)
        data = calculate(rows, payload)
        self.assertIsInstance(data, dict)
        self.assertTrue(data)
        result = run(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["project"], "benefit-eligibility-replay")

        assert result["decisions"][0]["eligible"] is False

    def test_version_metadata_is_semver_string(self):
        version = benefit_eligibility_replay.__version__
        self.assertIsInstance(version, str)
        parts = version.split(".")
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(part.isdigit() for part in parts))

    def test_cli_output_is_canonical_sorted_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_main([])
        text = buf.getvalue()
        # json.loads must accept the emitted document and all object keys
        # must be byte-sorted, so output is identical on every machine.
        parsed = json.loads(text)
        self.assertEqual(text, json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def test_cli_default_fixture_matches_pipeline(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_main([])
        self.assertEqual(json.loads(buf.getvalue()), run(fixture()))
