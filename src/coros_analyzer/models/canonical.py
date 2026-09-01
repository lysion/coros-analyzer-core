from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from math import isfinite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from coros_analyzer.models.provenance import DataSource, MetricValue


CANONICAL_SCHEMA_VERSION = 1
CANONICAL_MANIFEST_VERSION = 1
ACTIVITY_MATCH_RULE_VERSION = "activity_match_v2_binding_first"
FIT_PROJECTION_POLICY_VERSION = "fit_recorded_offset_then_configured_v1"
CANONICAL_ACTIVITIES_FILENAME = "canonical_activities.jsonl"
CANONICAL_DAILY_STATES_FILENAME = "canonical_daily_states.jsonl"
CANONICAL_MANIFEST_FILENAME = "canonical_manifest.json"


def _validate_nonempty_string(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class GeneratorIdentity:
    name: str
    version: str

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.name, "generator name")
        _validate_nonempty_string(self.version, "generator version")


@dataclass(frozen=True)
class CanonicalTimezoneConfig:
    configured: str
    fit_projection_policy: str

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.configured, "configured timezone")
        try:
            ZoneInfo(self.configured)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(
                f"configured timezone must be a valid IANA timezone: "
                f"{self.configured!r}"
            ) from error
        _validate_nonempty_string(
            self.fit_projection_policy, "FIT projection policy"
        )


@dataclass(frozen=True)
class SourceInclusion:
    source: DataSource
    included: bool

    def __post_init__(self) -> None:
        if not isinstance(self.source, DataSource):
            raise TypeError("source must be a DataSource")
        if type(self.included) is not bool:
            raise TypeError("included must be a bool")


@dataclass(frozen=True)
class EntityResolutionConfig:
    activity_rule_version: str

    def __post_init__(self) -> None:
        _validate_nonempty_string(
            self.activity_rule_version, "activity rule version"
        )


@dataclass(frozen=True)
class ManifestDeviceUnavailableInterval:
    start: date
    end: date

    def __post_init__(self) -> None:
        if type(self.start) is not date or type(self.end) is not date:
            raise TypeError("device-unavailable interval bounds must be dates")
        if self.start > self.end:
            raise ValueError(
                "device-unavailable interval start must not follow end"
            )


@dataclass(frozen=True)
class CanonicalFileInventory:
    path: str
    row_count: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.path, "canonical file path")
        if self.path not in {
            CANONICAL_ACTIVITIES_FILENAME,
            CANONICAL_DAILY_STATES_FILENAME,
        }:
            raise ValueError(
                "canonical file path must be an expected sibling filename"
            )
        if type(self.row_count) is not int:
            raise TypeError("row_count must be an integer")
        if self.row_count < 0:
            raise ValueError("row_count must be nonnegative")
        if not isinstance(self.sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", self.sha256
        ) is None:
            raise ValueError(
                "sha256 must be 64 lowercase hexadecimal characters"
            )


@dataclass(frozen=True)
class CanonicalManifest:
    manifest_version: int
    canonical_schema_version: int
    generator: GeneratorIdentity
    generated_at: datetime
    timezone: CanonicalTimezoneConfig
    sources: tuple[SourceInclusion, ...]
    entity_resolution: EntityResolutionConfig
    device_unavailable_intervals: tuple[
        ManifestDeviceUnavailableInterval, ...
    ]
    activities_file: CanonicalFileInventory
    daily_states_file: CanonicalFileInventory

    def __post_init__(self) -> None:
        if type(self.manifest_version) is not int:
            raise TypeError("manifest_version must be an integer")
        if self.manifest_version != CANONICAL_MANIFEST_VERSION:
            raise ValueError("unsupported canonical manifest version")
        if type(self.canonical_schema_version) is not int:
            raise TypeError("canonical_schema_version must be an integer")
        if self.canonical_schema_version != CANONICAL_SCHEMA_VERSION:
            raise ValueError("unsupported canonical schema version")
        if not isinstance(self.generator, GeneratorIdentity):
            raise TypeError("generator must be a GeneratorIdentity")
        if not isinstance(self.generated_at, datetime):
            raise TypeError("generated_at must be a datetime")
        if (
            self.generated_at.tzinfo is None
            or self.generated_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("generated_at must be timezone-aware UTC")
        if not isinstance(self.timezone, CanonicalTimezoneConfig):
            raise TypeError("timezone must be a CanonicalTimezoneConfig")
        if type(self.sources) is not tuple:
            raise TypeError("sources must be a tuple")
        if any(not isinstance(item, SourceInclusion) for item in self.sources):
            raise TypeError("sources must contain SourceInclusion values")
        source_values = [item.source for item in self.sources]
        if len(source_values) != len(set(source_values)):
            raise ValueError("sources must not contain duplicate source values")
        if set(source_values) != {DataSource.COROS_MCP, DataSource.FIT}:
            raise ValueError("sources must include COROS MCP and FIT")
        if not isinstance(self.entity_resolution, EntityResolutionConfig):
            raise TypeError(
                "entity_resolution must be an EntityResolutionConfig"
            )
        if type(self.device_unavailable_intervals) is not tuple:
            raise TypeError("device_unavailable_intervals must be a tuple")
        if any(
            not isinstance(item, ManifestDeviceUnavailableInterval)
            for item in self.device_unavailable_intervals
        ):
            raise TypeError(
                "device_unavailable_intervals must contain manifest intervals"
            )
        interval_values = [
            (item.start, item.end)
            for item in self.device_unavailable_intervals
        ]
        if len(interval_values) != len(set(interval_values)):
            raise ValueError(
                "device_unavailable_intervals must not contain duplicates"
            )
        if not isinstance(self.activities_file, CanonicalFileInventory):
            raise TypeError(
                "activities_file must be a CanonicalFileInventory"
            )
        if not isinstance(self.daily_states_file, CanonicalFileInventory):
            raise TypeError(
                "daily_states_file must be a CanonicalFileInventory"
            )
        if self.activities_file.path != CANONICAL_ACTIVITIES_FILENAME:
            raise ValueError("activities_file has the wrong canonical path")
        if self.daily_states_file.path != CANONICAL_DAILY_STATES_FILENAME:
            raise ValueError("daily_states_file has the wrong canonical path")


class EntityResolutionStatus(str, Enum):
    MATCHED = "matched"
    SOURCE_ONLY = "source_only"
    AMBIGUOUS = "ambiguous"


def _validate_nonnegative_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")


@dataclass(frozen=True)
class MatchedPairEvidence:
    start_time_delta_s: float
    duration_delta_s: float | None
    distance_delta_m: float | None

    def __post_init__(self) -> None:
        _validate_nonnegative_finite(
            self.start_time_delta_s, "start_time_delta_s"
        )
        if self.duration_delta_s is not None:
            _validate_nonnegative_finite(
                self.duration_delta_s, "duration_delta_s"
            )
        if self.distance_delta_m is not None:
            _validate_nonnegative_finite(
                self.distance_delta_m, "distance_delta_m"
            )


@dataclass(frozen=True)
class EntityResolutionEvidence:
    rule_version: str
    status: EntityResolutionStatus
    source_count: int
    qualifying_candidate_count: int
    counterpart_max_qualifying_candidate_count: int
    comparison: MatchedPairEvidence | None

    def __post_init__(self) -> None:
        if not isinstance(self.rule_version, str) or not self.rule_version.strip():
            raise ValueError("rule_version must be a non-empty string")
        if not isinstance(self.status, EntityResolutionStatus):
            raise TypeError("status must be an EntityResolutionStatus")
        for name in (
            "source_count",
            "qualifying_candidate_count",
            "counterpart_max_qualifying_candidate_count",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.source_count < 1:
            raise ValueError("source_count must be at least 1")

        if self.status is EntityResolutionStatus.MATCHED:
            if (
                self.source_count != 2
                or self.qualifying_candidate_count != 1
                or self.counterpart_max_qualifying_candidate_count != 1
                or self.comparison is None
            ):
                raise ValueError("invalid matched entity-resolution evidence")
        elif self.status is EntityResolutionStatus.SOURCE_ONLY:
            if (
                self.source_count != 1
                or self.qualifying_candidate_count != 0
                or self.counterpart_max_qualifying_candidate_count != 0
                or self.comparison is not None
            ):
                raise ValueError("invalid source-only entity-resolution evidence")
        elif (
            self.source_count != 1
            or self.qualifying_candidate_count < 1
            or self.counterpart_max_qualifying_candidate_count < 1
            or (
                self.qualifying_candidate_count == 1
                and self.counterpart_max_qualifying_candidate_count == 1
            )
            or self.comparison is not None
        ):
            raise ValueError("invalid ambiguous entity-resolution evidence")


class DeviceAvailability(str, Enum):
    AVAILABLE_OR_UNKNOWN = "available_or_unknown"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Activity:
    activity_id: str
    local_date: date
    start_time: datetime
    end_time: datetime | None
    sport: str
    sub_sport: str | None
    coros_label_id: str | None
    coros_sport_type: int | None
    fit_file_sha256: str | None
    timer_duration_s: MetricValue[float]
    elapsed_duration_s: MetricValue[float]
    distance_m: MetricValue[float]
    average_hr_bpm: MetricValue[float]
    elevation_gain_m: MetricValue[float]
    elevation_loss_m: MetricValue[float]
    coros_training_load: MetricValue[float]
    coros_calories_kcal: MetricValue[float]
    # None is permitted only before entity resolution or for explicit legacy-v0
    # loading. A serializable schema-v1 Activity requires complete evidence.
    entity_resolution: EntityResolutionEvidence | None = None

    def __post_init__(self) -> None:
        if self.start_time.tzinfo is None:
            raise ValueError("activity start_time must be timezone-aware")
        if self.end_time is not None and self.end_time.tzinfo is None:
            raise ValueError("activity end_time must be timezone-aware")


@dataclass(frozen=True)
class DailyState:
    local_date: date
    device_availability: DeviceAvailability
    resting_hr_bpm: MetricValue[float]
    sleep_duration_min: MetricValue[float]
    sleep_score: MetricValue[float]
    sleep_hrv_avg_ms: MetricValue[float]
    sleep_hrv_vendor_baseline_ms: MetricValue[float]
