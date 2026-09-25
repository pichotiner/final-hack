"""Smoke-check generated Pydantic transport models against the synthetic fixtures."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from pydantic import ValidationError
from generated.models import SourceEvent

DATA = Path(__file__).resolve().parents[1] / "data/scenarios.json"


class GeneratedModelTest(unittest.TestCase):
    def test_all_source_events_parse(self):
        scenarios = json.loads(DATA.read_text(encoding="utf-8"))["scenarios"]
        for scenario in scenarios:
            for event in [*scenario["events"], *scenario["late_events"]]:
                with self.subTest(event=event["event_id"]):
                    SourceEvent.model_validate(event)

    def test_rejects_unsupported_version_and_wrong_payload(self):
        event = json.loads(DATA.read_text(encoding="utf-8"))["scenarios"][0]["events"][0]
        with self.assertRaises(ValidationError):
            SourceEvent.model_validate({**event, "schema_version": "9.0"})
        with self.assertRaises(ValidationError):
            SourceEvent.model_validate({**event, "event_type": "inspection_observed"})


if __name__ == "__main__":
    unittest.main()
