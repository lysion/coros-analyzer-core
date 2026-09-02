from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2

from coros_analyzer import (
    build_canonical_dataset,
    read_activities_jsonl,
    read_canonical_manifest,
    read_daily_states_jsonl,
    validate_canonical_dataset,
)
from coros_analyzer.models.canonical import (
    CANONICAL_ACTIVITIES_FILENAME,
    CANONICAL_DAILY_STATES_FILENAME,
    CANONICAL_MANIFEST_FILENAME,
    EntityResolutionStatus,
)
from coros_analyzer.models.provenance import (
    DataAvailability,
    DataSource,
    MetricOrigin,
)


def test_synthetic_public_e2e_build_validate_and_readback(tmp_path: Path) -> None:
    fixture_root = Path(__file__).parent / "fixtures" / "synthetic" / "basic"
    stage1_dir = tmp_path / "stage1"
    fit_dir = tmp_path / "fit"
    stage1_dir.mkdir()
    fit_dir.mkdir()

    copy2(fixture_root / "activities.csv", stage1_dir / "activities.csv")
    copy2(
        fixture_root / "daily_recovery.csv",
        stage1_dir / "daily_recovery.csv",
    )

    timings = build_canonical_dataset(
        stage1_dir=stage1_dir,
        fit_dir=fit_dir,
        configured_timezone_name="UTC",
        generated_at=datetime(2044, 1, 2, tzinfo=UTC),
    )

    manifest_path = stage1_dir / CANONICAL_MANIFEST_FILENAME
    activities_path = stage1_dir / CANONICAL_ACTIVITIES_FILENAME
    daily_states_path = stage1_dir / CANONICAL_DAILY_STATES_FILENAME

    manifest = validate_canonical_dataset(manifest_path)
    activities = read_activities_jsonl(activities_path)
    daily_states = read_daily_states_jsonl(daily_states_path)
    reread_manifest = read_canonical_manifest(manifest_path)

    assert timings["canonical_activities_rows"] == 1
    assert timings["canonical_daily_states_rows"] == 1
    assert timings["canonical_unmatched_fit_activities"] == 0

    assert manifest == reread_manifest
    assert manifest.generator.name == "coros-analyzer-core"
    assert manifest.generator.version
    assert manifest.activities_file.row_count == 1
    assert manifest.daily_states_file.row_count == 1

    activity = activities[0]
    assert activity.activity_id == "coros:fictional-run-20440101:1"
    assert activity.local_date.isoformat() == "2044-01-01"
    assert activity.fit_file_sha256 is None
    assert activity.entity_resolution is not None
    assert activity.entity_resolution.status is EntityResolutionStatus.SOURCE_ONLY
    assert activity.entity_resolution.comparison is None
    assert activity.distance_m.value == 5000.0
    assert activity.distance_m.source is DataSource.COROS_MCP
    assert activity.distance_m.origin is MetricOrigin.SOURCE_SUMMARY
    assert activity.distance_m.availability is DataAvailability.PRESENT
    assert activity.coros_training_load.value == 42.0
    assert activity.coros_training_load.origin is MetricOrigin.COROS_VENDOR

    daily_state = daily_states[0]
    assert daily_state.local_date.isoformat() == "2044-01-01"
    assert daily_state.resting_hr_bpm.value == 48.0
    assert daily_state.resting_hr_bpm.source is DataSource.COROS_MCP
    assert daily_state.resting_hr_bpm.origin is MetricOrigin.SOURCE_SUMMARY
    assert daily_state.sleep_score.value == 86.0
    assert daily_state.sleep_score.origin is MetricOrigin.COROS_VENDOR
