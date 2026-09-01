"""Curated public API for coros-analyzer."""

from coros_analyzer.canonical import (
    build_canonical_dataset,
    read_activities_jsonl,
    read_canonical_manifest,
    read_daily_states_jsonl,
    validate_canonical_dataset,
)
from coros_analyzer.models.canonical import (
    CANONICAL_MANIFEST_VERSION,
    CANONICAL_SCHEMA_VERSION,
    Activity,
    CanonicalManifest,
    DailyState,
    DeviceAvailability,
    EntityResolutionEvidence,
    EntityResolutionStatus,
    MatchedPairEvidence,
)
from coros_analyzer.models.provenance import (
    DataAvailability,
    DataSource,
    MetricOrigin,
    MetricValue,
)


__all__ = [
    "Activity",
    "CANONICAL_MANIFEST_VERSION",
    "CANONICAL_SCHEMA_VERSION",
    "CanonicalManifest",
    "DailyState",
    "DataAvailability",
    "DataSource",
    "DeviceAvailability",
    "EntityResolutionEvidence",
    "EntityResolutionStatus",
    "MatchedPairEvidence",
    "MetricOrigin",
    "MetricValue",
    "build_canonical_dataset",
    "read_activities_jsonl",
    "read_canonical_manifest",
    "read_daily_states_jsonl",
    "validate_canonical_dataset",
]
