"""Compare the executable example against separately authored expectations."""

import json
from pathlib import Path

import pytest

from scripts.production_improvements_demo import demonstrate


def test_demo_matches_independent_expected():
    expected = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/acceptance/production-improvements-expected.json"
        ).read_text()
    )
    report = demonstrate()
    actual = {
        "synthetic": True,
        "scope": "pure_rules_not_runtime",
        "health_ready": report["self_diagnosis"]["ready"]["status"],
        "health_covered_camera": report["self_diagnosis"]["covered_camera"]["status"],
        "health_expired": report["self_diagnosis"]["expired"]["status"],
        "tool": report["tool_passport"]["tool"],
        "old_tool_baseline_applicable": report["tool_passport"]["baseline_applicable"],
        "lot_exact": report["lot_quarantine_proposal"]["exact"],
        "lot_possible": report["lot_quarantine_proposal"]["possible"],
        "created_confirmed_defects": report["lot_quarantine_proposal"]["confirmed_defects_created"],
        "recommended_method": report["next_check"]["methods"][0]["method_id"],
        "handoff_accepted": report["handoff"]["accepted"],
        "active_after_100_duplicates": report["alarms_before_ack"]["active"],
        "sounds_before_ack": report["alarms_before_ack"]["sound_tokens"],
        "sounds_after_ack": report["alarms_after_ack"]["sound_tokens"],
        "active_after_ack": report["alarms_after_ack"]["active"],
        "offline_light": report["offline_monitoring"]["light"],
        "ewma": [row["ewma"] for row in report["ewma"]],
        "ewma_status": [row["status"] for row in report["ewma"]],
        "exposure_before": report["exposure_before"]["exact"],
        "exposure_after": report["exposure_after"]["exact"],
        "stage_processed": report["stage_map"][0]["processed"],
        "stage_coverage": report["stage_map"][0]["coverage"],
        "stage_discovery_rate": report["stage_map"][0]["discovery_rate"],
        "stage_established_origin_rate": report["stage_map"][0]["established_origin_rate"],
    }
    assert actual.pop("ewma") == pytest.approx(expected.pop("ewma"))
    assert actual == expected
