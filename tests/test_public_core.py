from __future__ import annotations

from datetime import UTC, date, datetime

import coros_analyzer
from coros_analyzer.fit_binding import (
    EXACT_DOWNLOAD_ORIGIN,
    FitAcquisitionBinding,
    load_acquisition_bindings,
    upsert_acquisition_binding,
)
from coros_analyzer.models.canonical import (
    ACTIVITY_MATCH_RULE_VERSION,
    CANONICAL_ACTIVITIES_FILENAME,
    CANONICAL_DAILY_STATES_FILENAME,
    FIT_PROJECTION_POLICY_VERSION,
    CanonicalFileInventory,
    CanonicalManifest,
    CanonicalTimezoneConfig,
    EntityResolutionConfig,
    GeneratorIdentity,
    SourceInclusion,
)
from coros_analyzer.models.provenance import DataSource
from coros_analyzer.sport import normalized_sport_family


def test_public_surface_is_explicit_and_has_no_cli() -> None:
    assert "validate_canonical_dataset" in coros_analyzer.__all__
    assert "coros_analyzer.cli" not in __import__("sys").modules


def test_synthetic_binding_round_trip(tmp_path) -> None:
    binding = FitAcquisitionBinding(
        "a" * 64,
        "synthetic-activity",
        900,
        EXACT_DOWNLOAD_ORIGIN,
    )
    upsert_acquisition_binding(tmp_path, binding)
    assert load_acquisition_bindings(tmp_path) == (binding,)


def test_manifest_model_accepts_constructed_synthetic_metadata() -> None:
    inventory = CanonicalFileInventory(
        CANONICAL_ACTIVITIES_FILENAME, 0, "b" * 64
    )
    manifest = CanonicalManifest(
        manifest_version=1,
        canonical_schema_version=1,
        generator=GeneratorIdentity("coros-analyzer-core", "0.1.0"),
        generated_at=datetime(2044, 1, 1, tzinfo=UTC),
        timezone=CanonicalTimezoneConfig("UTC", FIT_PROJECTION_POLICY_VERSION),
        sources=(
            SourceInclusion(DataSource.COROS_MCP, False),
            SourceInclusion(DataSource.FIT, False),
        ),
        entity_resolution=EntityResolutionConfig(ACTIVITY_MATCH_RULE_VERSION),
        device_unavailable_intervals=(),
        activities_file=inventory,
        daily_states_file=CanonicalFileInventory(
            CANONICAL_DAILY_STATES_FILENAME, 0, "c" * 64
        ),
    )
    assert manifest.generated_at.date() == date(2044, 1, 1)


def test_sport_normalization_is_data_free() -> None:
    assert normalized_sport_family("Trail Running") == "running"
    assert normalized_sport_family("Road Cycling") == "cycling"
