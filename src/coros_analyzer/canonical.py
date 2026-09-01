from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, replace
from datetime import UTC, date, datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter
from typing import Any, Iterator, TypeVar
from zoneinfo import ZoneInfo

from coros_analyzer.fit_binding import (
    FitAcquisitionBinding,
    bootstrap_legacy_managed_bindings,
)
from coros_analyzer.models.canonical import (
    ACTIVITY_MATCH_RULE_VERSION,
    Activity,
    CANONICAL_ACTIVITIES_FILENAME,
    CANONICAL_DAILY_STATES_FILENAME,
    CANONICAL_MANIFEST_FILENAME,
    CANONICAL_MANIFEST_VERSION,
    CANONICAL_SCHEMA_VERSION,
    FIT_PROJECTION_POLICY_VERSION,
    CanonicalFileInventory,
    CanonicalManifest,
    CanonicalTimezoneConfig,
    DailyState,
    DeviceAvailability,
    EntityResolutionConfig,
    EntityResolutionEvidence,
    EntityResolutionStatus,
    GeneratorIdentity,
    ManifestDeviceUnavailableInterval,
    MatchedPairEvidence,
    SourceInclusion,
)
from coros_analyzer.models.provenance import (
    DataAvailability,
    DataSource,
    MetricOrigin,
    MetricValue,
)
from coros_analyzer.sources.fit import FitDataSource, FitMessage
from coros_analyzer.sport import normalized_sport_family


DeviceUnavailableInterval = tuple[date, date]
_CanonicalRow = TypeVar("_CanonicalRow", Activity, DailyState)
_FitProjectionCacheKey = tuple[str, ZoneInfo, str]
_fit_projection_cache: ContextVar[
    dict[_FitProjectionCacheKey, Activity] | None
] = ContextVar("fit_projection_cache", default=None)
_FIT_PROJECTION_CACHE_FORMAT_VERSION = 1
_FIT_PROJECTION_CACHE_RELATIVE_PATH = Path(
    ".coros-analyzer-cache/fit-projections"
)

ACTIVITY_METRIC_NAMES = {
    "timer_duration_s",
    "elapsed_duration_s",
    "distance_m",
    "average_hr_bpm",
    "elevation_gain_m",
    "elevation_loss_m",
    "coros_training_load",
    "coros_calories_kcal",
}


def absent_metric(
    source: DataSource,
    origin: MetricOrigin,
    unit: str | None = None,
) -> MetricValue[Any]:
    return MetricValue(
        value=None,
        source=source,
        origin=origin,
        unit=unit,
        availability=DataAvailability.ABSENT,
    )


def metric_from_value(
    value: Any,
    source: DataSource,
    origin: MetricOrigin,
    unit: str | None = None,
) -> MetricValue[Any]:
    if value is None or value == "":
        return absent_metric(source, origin, unit)
    if isinstance(value, str) and value.lower() == "null":
        return MetricValue(
            value=None,
            source=source,
            origin=origin,
            unit=unit,
            availability=DataAvailability.NULL,
        )
    return MetricValue(
        value=float(value),
        source=source,
        origin=origin,
        unit=unit,
    )


def _fields(message: FitMessage) -> dict[str, Any]:
    return {field.name: field.value for field in message.fields}


def _first_message(
    messages: list[FitMessage], name: str
) -> dict[str, Any] | None:
    message = next((message for message in messages if message.name == name), None)
    return _fields(message) if message is not None else None


def _fit_timezone(
    messages: list[FitMessage], configured_timezone: ZoneInfo
) -> timezone | ZoneInfo:
    activity = _first_message(messages, "activity") or {}
    timestamp = activity.get("timestamp")
    local_timestamp = activity.get("local_timestamp")
    if isinstance(timestamp, datetime) and isinstance(local_timestamp, datetime):
        utc_timestamp = _as_utc(timestamp)
        local_wall_time = local_timestamp.replace(tzinfo=None)
        offset = local_wall_time - utc_timestamp.replace(tzinfo=None)
        if -timedelta(hours=24) < offset < timedelta(hours=24):
            return timezone(offset)
    return configured_timezone


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@contextmanager
def _fit_projection_cache_scope() -> Iterator[None]:
    token = _fit_projection_cache.set({})
    try:
        yield
    finally:
        _fit_projection_cache.reset(token)


def project_fit_activity(
    fit_file: Path,
    configured_timezone: ZoneInfo,
    data_source: FitDataSource | None = None,
    *,
    persistent_cache_dir: Path | None = None,
    cache_metrics: dict[str, int] | None = None,
) -> Activity:
    file_hash = hashlib.sha256(fit_file.read_bytes()).hexdigest()
    cache = _fit_projection_cache.get()
    cache_key = (
        file_hash,
        configured_timezone,
        FIT_PROJECTION_POLICY_VERSION,
    )
    if data_source is None and cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            if cache_metrics is not None:
                cache_metrics["fit_projection_cache_hits"] += 1
            return cached

    if data_source is None and persistent_cache_dir is not None:
        cached = _read_persistent_fit_projection(
            persistent_cache_dir, cache_key
        )
        if cached is not None:
            if cache is not None:
                cache[cache_key] = cached
            if cache_metrics is not None:
                cache_metrics["fit_projection_cache_hits"] += 1
            return cached

    if cache_metrics is not None:
        cache_metrics["fit_projection_cache_misses"] += 1
        cache_metrics["fit_files_decoded"] += 1

    messages = (data_source or FitDataSource()).read(fit_file)
    session = _first_message(messages, "session")
    if session is None:
        raise ValueError(f"FIT file has no session message: {fit_file}")
    start = session.get("start_time")
    if not isinstance(start, datetime):
        raise ValueError(f"FIT session has no start_time: {fit_file}")

    activity_timezone = _fit_timezone(messages, configured_timezone)
    start_time = _as_utc(start).astimezone(activity_timezone)
    elapsed = session.get("total_elapsed_time")
    end_time = (
        start_time + timedelta(seconds=float(elapsed))
        if elapsed is not None
        else None
    )
    source = DataSource.FIT
    summary = MetricOrigin.SOURCE_SUMMARY
    vendor = MetricOrigin.COROS_VENDOR
    activity = Activity(
        activity_id=f"fit:{file_hash}",
        local_date=start_time.date(),
        start_time=start_time,
        end_time=end_time,
        sport=str(session.get("sport") or "unknown"),
        sub_sport=(
            str(session["sub_sport"])
            if session.get("sub_sport") is not None
            else None
        ),
        coros_label_id=None,
        coros_sport_type=None,
        fit_file_sha256=file_hash,
        timer_duration_s=metric_from_value(
            session.get("total_timer_time"), source, summary, "s"
        ),
        elapsed_duration_s=metric_from_value(elapsed, source, summary, "s"),
        distance_m=metric_from_value(
            session.get("total_distance"), source, summary, "m"
        ),
        average_hr_bpm=metric_from_value(
            session.get("avg_heart_rate"), source, summary, "bpm"
        ),
        elevation_gain_m=metric_from_value(
            session.get("total_ascent"), source, summary, "m"
        ),
        elevation_loss_m=metric_from_value(
            session.get("total_descent"), source, summary, "m"
        ),
        coros_training_load=absent_metric(
            DataSource.COROS_MCP, vendor
        ),
        coros_calories_kcal=absent_metric(
            DataSource.COROS_MCP, vendor, "kcal"
        ),
    )
    if data_source is None and persistent_cache_dir is not None:
        _write_persistent_fit_projection(
            persistent_cache_dir, cache_key, activity
        )
    if data_source is None and cache is not None:
        cache[cache_key] = activity
    return activity


def _is_device_unavailable(
    local_date: date,
    intervals: Sequence[DeviceUnavailableInterval],
) -> bool:
    unavailable = False
    for start_date, end_date in intervals:
        if start_date > end_date:
            raise ValueError(
                "device-unavailable interval start date must not follow end date"
            )
        if start_date <= local_date <= end_date:
            unavailable = True
    return unavailable


def project_daily_state(
    row: dict[str, Any],
    device_unavailable_intervals: Sequence[DeviceUnavailableInterval] = (),
) -> DailyState:
    local_date = date.fromisoformat(str(row["date"]))
    source = DataSource.COROS_MCP
    summary = MetricOrigin.SOURCE_SUMMARY
    vendor = MetricOrigin.COROS_VENDOR
    unavailable = _is_device_unavailable(
        local_date, device_unavailable_intervals
    )
    return DailyState(
        local_date=local_date,
        device_availability=(
            DeviceAvailability.UNAVAILABLE
            if unavailable
            else DeviceAvailability.AVAILABLE_OR_UNKNOWN
        ),
        resting_hr_bpm=metric_from_value(
            row.get("resting_hr_bpm"), source, summary, "bpm"
        ),
        sleep_duration_min=metric_from_value(
            row.get("sleep_duration_min"), source, summary, "min"
        ),
        sleep_score=metric_from_value(
            row.get("sleep_score"), source, vendor
        ),
        sleep_hrv_avg_ms=metric_from_value(
            row.get("sleep_hrv_avg_ms"), source, summary, "ms"
        ),
        sleep_hrv_vendor_baseline_ms=metric_from_value(
            row.get("sleep_hrv_vendor_baseline_ms"), source, vendor, "ms"
        ),
    )


def _present_number(metric: MetricValue[float]) -> float | None:
    if metric.availability is not DataAvailability.PRESENT:
        return None
    return float(metric.value) if metric.value is not None else None


@dataclass(frozen=True)
class _QualifyingPairComparison:
    start_time_delta_s: float
    duration_delta_s: float | None
    distance_delta_m: float | None


@dataclass(frozen=True)
class _ResolutionResult:
    status: EntityResolutionStatus
    qualifying_candidate_count: int
    counterpart_max_qualifying_candidate_count: int
    matched_counterpart_index: int | None
    matched_comparison: _QualifyingPairComparison | None


@dataclass(frozen=True)
class _ActivityMatchPlan:
    fit_results: tuple[_ResolutionResult, ...]
    coros_results: tuple[_ResolutionResult, ...]


def _entity_resolution_evidence(
    resolution: _ResolutionResult,
) -> EntityResolutionEvidence:
    comparison = resolution.matched_comparison
    return EntityResolutionEvidence(
        rule_version=ACTIVITY_MATCH_RULE_VERSION,
        status=resolution.status,
        source_count=(
            2 if resolution.status is EntityResolutionStatus.MATCHED else 1
        ),
        qualifying_candidate_count=resolution.qualifying_candidate_count,
        counterpart_max_qualifying_candidate_count=(
            resolution.counterpart_max_qualifying_candidate_count
        ),
        comparison=(
            MatchedPairEvidence(
                start_time_delta_s=comparison.start_time_delta_s,
                duration_delta_s=comparison.duration_delta_s,
                distance_delta_m=comparison.distance_delta_m,
            )
            if comparison is not None
            else None
        ),
    )


def _qualifying_pair_comparison(
    activity: Activity, row: dict[str, Any]
) -> _QualifyingPairComparison | None:
    try:
        if normalized_sport_family(activity.sport) != normalized_sport_family(
            str(row["sport_name"])
        ):
            return None
        start_timestamp = row.get("start_timestamp")
        if start_timestamp in {None, ""}:
            return None
        start_time_delta_s = abs(
            activity.start_time.timestamp() - float(start_timestamp)
        )
        if not isfinite(start_time_delta_s) or start_time_delta_s > 10:
            return None

        duration_delta_s = None
        fit_duration = _present_number(activity.timer_duration_s)
        if fit_duration is not None and row.get("duration_s") not in {None, ""}:
            duration_delta_s = abs(fit_duration - float(row["duration_s"]))
            if not isfinite(duration_delta_s) or duration_delta_s > 10:
                return None
        distance_delta_m = None
        fit_distance = _present_number(activity.distance_m)
        if fit_distance is not None and row.get("distance_m") not in {None, ""}:
            distance_delta_m = abs(fit_distance - float(row["distance_m"]))
            tolerance = max(100.0, fit_distance * 0.02)
            if not isfinite(distance_delta_m) or distance_delta_m > tolerance:
                return None
        if duration_delta_s is None and distance_delta_m is None:
            return None
        return _QualifyingPairComparison(
            start_time_delta_s=start_time_delta_s,
            duration_delta_s=duration_delta_s,
            distance_delta_m=distance_delta_m,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _bound_pair_comparison(
    activity: Activity, row: dict[str, Any]
) -> _QualifyingPairComparison:
    start_time_delta_s = abs(
        activity.start_time.timestamp() - float(row["start_timestamp"])
    )
    duration_delta_s = None
    fit_duration = _present_number(activity.timer_duration_s)
    if fit_duration is not None and row.get("duration_s") not in {None, ""}:
        duration_delta_s = abs(fit_duration - float(row["duration_s"]))
    distance_delta_m = None
    fit_distance = _present_number(activity.distance_m)
    if fit_distance is not None and row.get("distance_m") not in {None, ""}:
        distance_delta_m = abs(fit_distance - float(row["distance_m"]))
    for value in (start_time_delta_s, duration_delta_s, distance_delta_m):
        if value is not None and not isfinite(value):
            raise ValueError(
                "bound activity comparison contains a non-finite delta"
            )
    return _QualifyingPairComparison(
        start_time_delta_s=start_time_delta_s,
        duration_delta_s=duration_delta_s,
        distance_delta_m=distance_delta_m,
    )


def _strong_match(activity: Activity, row: dict[str, Any]) -> bool:
    return _qualifying_pair_comparison(activity, row) is not None


def _resolution_result(
    candidates: dict[int, _QualifyingPairComparison],
    counterpart_degrees: list[int],
) -> _ResolutionResult:
    own_degree = len(candidates)
    if own_degree == 0:
        return _ResolutionResult(
            status=EntityResolutionStatus.SOURCE_ONLY,
            qualifying_candidate_count=0,
            counterpart_max_qualifying_candidate_count=0,
            matched_counterpart_index=None,
            matched_comparison=None,
        )
    counterpart_max_degree = max(
        counterpart_degrees[index] for index in candidates
    )
    if own_degree == 1 and counterpart_max_degree == 1:
        counterpart_index, comparison = next(iter(candidates.items()))
        return _ResolutionResult(
            status=EntityResolutionStatus.MATCHED,
            qualifying_candidate_count=1,
            counterpart_max_qualifying_candidate_count=1,
            matched_counterpart_index=counterpart_index,
            matched_comparison=comparison,
        )
    return _ResolutionResult(
        status=EntityResolutionStatus.AMBIGUOUS,
        qualifying_candidate_count=own_degree,
        counterpart_max_qualifying_candidate_count=counterpart_max_degree,
        matched_counterpart_index=None,
        matched_comparison=None,
    )


def _activity_match_plan(
    fit_activities: list[Activity],
    coros_rows: list[dict[str, Any]],
) -> _ActivityMatchPlan:
    fit_edges: list[dict[int, _QualifyingPairComparison]] = []
    coros_edges: list[dict[int, _QualifyingPairComparison]] = [
        {} for _ in coros_rows
    ]
    for fit_index, activity in enumerate(fit_activities):
        candidates: dict[int, _QualifyingPairComparison] = {}
        for coros_index, row in enumerate(coros_rows):
            comparison = _qualifying_pair_comparison(activity, row)
            if comparison is None:
                continue
            candidates[coros_index] = comparison
            coros_edges[coros_index][fit_index] = comparison
        fit_edges.append(candidates)

    fit_degrees = [len(candidates) for candidates in fit_edges]
    coros_degrees = [len(candidates) for candidates in coros_edges]
    return _ActivityMatchPlan(
        fit_results=tuple(
            _resolution_result(candidates, coros_degrees)
            for candidates in fit_edges
        ),
        coros_results=tuple(
            _resolution_result(candidates, fit_degrees)
            for candidates in coros_edges
        ),
    )


def _binding_first_activity_match_plan(
    fit_activities: list[Activity],
    coros_rows: list[dict[str, Any]],
    bindings: Sequence[FitAcquisitionBinding],
) -> tuple[_ActivityMatchPlan, int]:
    fit_by_sha = {
        activity.fit_file_sha256: index
        for index, activity in enumerate(fit_activities)
        if activity.fit_file_sha256 is not None
    }
    coros_by_identity: dict[tuple[str, int], int] = {}
    for index, row in enumerate(coros_rows):
        identity = (str(row["label_id"]), int(row["sport_type"]))
        if identity in coros_by_identity:
            raise ValueError(f"duplicate COROS activity identity: {identity!r}")
        coros_by_identity[identity] = index

    fit_results: list[_ResolutionResult | None] = [None] * len(fit_activities)
    coros_results: list[_ResolutionResult | None] = [None] * len(coros_rows)
    consistency_warnings = 0
    for binding in bindings:
        fit_index = fit_by_sha.get(binding.fit_file_sha256)
        coros_index = coros_by_identity.get(binding.coros_identity)
        source_only = _ResolutionResult(
            EntityResolutionStatus.SOURCE_ONLY, 0, 0, None, None
        )
        if fit_index is not None and coros_index is not None:
            comparison = _bound_pair_comparison(
                fit_activities[fit_index], coros_rows[coros_index]
            )
            matched = _ResolutionResult(
                EntityResolutionStatus.MATCHED,
                1,
                1,
                coros_index,
                comparison,
            )
            fit_results[fit_index] = matched
            coros_results[coros_index] = _ResolutionResult(
                EntityResolutionStatus.MATCHED,
                1,
                1,
                fit_index,
                comparison,
            )
            if _qualifying_pair_comparison(
                fit_activities[fit_index], coros_rows[coros_index]
            ) is None:
                consistency_warnings += 1
        elif fit_index is not None:
            fit_results[fit_index] = source_only
        elif coros_index is not None:
            coros_results[coros_index] = source_only

    unbound_fit_indexes = [
        index for index, result in enumerate(fit_results) if result is None
    ]
    unbound_coros_indexes = [
        index for index, result in enumerate(coros_results) if result is None
    ]
    fallback = _activity_match_plan(
        [fit_activities[index] for index in unbound_fit_indexes],
        [coros_rows[index] for index in unbound_coros_indexes],
    )
    for local_index, result in enumerate(fallback.fit_results):
        global_counterpart = (
            unbound_coros_indexes[result.matched_counterpart_index]
            if result.matched_counterpart_index is not None
            else None
        )
        fit_results[unbound_fit_indexes[local_index]] = replace(
            result, matched_counterpart_index=global_counterpart
        )
    for local_index, result in enumerate(fallback.coros_results):
        global_counterpart = (
            unbound_fit_indexes[result.matched_counterpart_index]
            if result.matched_counterpart_index is not None
            else None
        )
        coros_results[unbound_coros_indexes[local_index]] = replace(
            result, matched_counterpart_index=global_counterpart
        )
    if any(result is None for result in (*fit_results, *coros_results)):
        raise AssertionError("activity resolution plan is incomplete")
    return (
        _ActivityMatchPlan(
            tuple(result for result in fit_results if result is not None),
            tuple(result for result in coros_results if result is not None),
        ),
        consistency_warnings,
    )


def _apply_activity_match_plan(
    fit_activities: list[Activity],
    coros_rows: list[dict[str, Any]],
    plan: _ActivityMatchPlan,
) -> tuple[list[Activity], set[int]]:
    matched_coros: set[int] = set()
    result = []
    for fit_index, activity in enumerate(fit_activities):
        resolution = plan.fit_results[fit_index]
        if resolution.status is not EntityResolutionStatus.MATCHED:
            result.append(
                replace(
                    activity,
                    entity_resolution=_entity_resolution_evidence(resolution),
                )
            )
            continue
        coros_index = resolution.matched_counterpart_index
        if coros_index is None:
            raise AssertionError("matched resolution has no counterpart")
        row = coros_rows[coros_index]
        matched_coros.add(coros_index)
        result.append(
            replace(
                activity,
                activity_id=f"coros:{row['label_id']}:{row['sport_type']}",
                coros_label_id=str(row["label_id"]),
                coros_sport_type=int(row["sport_type"]),
                coros_training_load=metric_from_value(
                    row.get("training_load"),
                    DataSource.COROS_MCP,
                    MetricOrigin.COROS_VENDOR,
                ),
                coros_calories_kcal=metric_from_value(
                    row.get("calories_kcal"),
                    DataSource.COROS_MCP,
                    MetricOrigin.COROS_VENDOR,
                    "kcal",
                ),
                entity_resolution=_entity_resolution_evidence(resolution),
            )
        )
    return result, matched_coros


def match_activities(
    fit_activities: list[Activity],
    coros_rows: list[dict[str, Any]],
    bindings: Sequence[FitAcquisitionBinding] = (),
) -> tuple[list[Activity], set[int]]:
    plan, _ = _binding_first_activity_match_plan(
        fit_activities, coros_rows, bindings
    )
    return _apply_activity_match_plan(fit_activities, coros_rows, plan)


def project_coros_activity(
    row: dict[str, Any], configured_timezone: ZoneInfo
) -> Activity:
    start = datetime.fromtimestamp(float(row["start_timestamp"]), UTC).astimezone(
        configured_timezone
    )
    end_value = row.get("end_timestamp")
    end = (
        datetime.fromtimestamp(float(end_value), UTC).astimezone(
            configured_timezone
        )
        if end_value not in {None, ""}
        else None
    )
    source = DataSource.COROS_MCP
    summary = MetricOrigin.SOURCE_SUMMARY
    vendor = MetricOrigin.COROS_VENDOR
    return Activity(
        activity_id=f"coros:{row['label_id']}:{row['sport_type']}",
        local_date=start.date(),
        start_time=start,
        end_time=end,
        sport=str(row["sport_name"]),
        sub_sport=None,
        coros_label_id=str(row["label_id"]),
        coros_sport_type=int(row["sport_type"]),
        fit_file_sha256=None,
        timer_duration_s=metric_from_value(
            row.get("duration_s"), source, summary, "s"
        ),
        elapsed_duration_s=absent_metric(source, summary, "s"),
        distance_m=metric_from_value(row.get("distance_m"), source, summary, "m"),
        average_hr_bpm=metric_from_value(
            row.get("avg_hr_bpm"), source, summary, "bpm"
        ),
        elevation_gain_m=metric_from_value(
            row.get("elevation_gain_m"), source, summary, "m"
        ),
        elevation_loss_m=metric_from_value(
            row.get("elevation_loss_m"), source, summary, "m"
        ),
        coros_training_load=metric_from_value(
            row.get("training_load"), source, vendor
        ),
        coros_calories_kcal=metric_from_value(
            row.get("calories_kcal"), source, vendor, "kcal"
        ),
    )


def build_canonical_activities(
    fit_files: list[Path],
    coros_rows: list[dict[str, Any]],
    configured_timezone: ZoneInfo,
    timings: dict[str, float] | None = None,
    persistent_cache_dir: Path | None = None,
    bindings: Sequence[FitAcquisitionBinding] = (),
) -> list[Activity]:
    fit_decode_started = perf_counter()
    cache_metrics = {
        "fit_files_total": len(fit_files),
        "fit_projection_cache_hits": 0,
        "fit_projection_cache_misses": 0,
        "fit_files_decoded": 0,
    }
    fit_activities_by_id: dict[str, Activity] = {}
    for path in sorted(fit_files):
        if persistent_cache_dir is None:
            activity = project_fit_activity(path, configured_timezone)
            cache_metrics["fit_projection_cache_misses"] += 1
            cache_metrics["fit_files_decoded"] += 1
        else:
            activity = project_fit_activity(
                path,
                configured_timezone,
                persistent_cache_dir=persistent_cache_dir,
                cache_metrics=cache_metrics,
            )
        fit_activities_by_id.setdefault(activity.activity_id, activity)
    if timings is not None:
        timings["fit_decode_seconds"] = perf_counter() - fit_decode_started
        timings.update(cache_metrics)
    fit_activities = list(fit_activities_by_id.values())
    matching_started = perf_counter()
    plan, consistency_warnings = _binding_first_activity_match_plan(
        fit_activities, coros_rows, bindings
    )
    matched, matched_coros = _apply_activity_match_plan(
        fit_activities, coros_rows, plan
    )
    if timings is not None:
        timings["matching_seconds"] = perf_counter() - matching_started
        timings["canonical_binding_consistency_warnings"] = (
            consistency_warnings
        )
    coros_projection_started = perf_counter()
    coros_activities = [
        replace(
            project_coros_activity(row, configured_timezone),
            entity_resolution=_entity_resolution_evidence(
                plan.coros_results[index]
            )
        )
        for index, row in enumerate(coros_rows)
        if index not in matched_coros
    ]
    if timings is not None:
        timings["coros_projection_seconds"] = (
            perf_counter() - coros_projection_started
        )
    result = [*matched, *coros_activities]
    unique = {activity.activity_id: activity for activity in result}
    resolved = sorted(
        unique.values(), key=lambda item: (item.start_time, item.activity_id)
    )
    if any(activity.entity_resolution is None for activity in resolved):
        raise AssertionError("canonical Activity has no entity-resolution evidence")
    return resolved


def _metric_to_dict(metric: MetricValue[Any]) -> dict[str, Any]:
    return {
        "value": metric.value,
        "source": metric.source.value,
        "origin": metric.origin.value,
        "unit": metric.unit,
        "availability": metric.availability.value,
    }


def _metric_from_dict(data: dict[str, Any]) -> MetricValue[Any]:
    return MetricValue(
        value=data["value"],
        source=DataSource(data["source"]),
        origin=MetricOrigin(data["origin"]),
        unit=data["unit"],
        availability=DataAvailability(data["availability"]),
    )


def _entity_resolution_to_dict(
    evidence: EntityResolutionEvidence,
) -> dict[str, Any]:
    comparison = evidence.comparison
    return {
        "rule_version": evidence.rule_version,
        "status": evidence.status.value,
        "source_count": evidence.source_count,
        "qualifying_candidate_count": evidence.qualifying_candidate_count,
        "counterpart_max_qualifying_candidate_count": (
            evidence.counterpart_max_qualifying_candidate_count
        ),
        "comparison": (
            {
                "start_time_delta_s": comparison.start_time_delta_s,
                "duration_delta_s": comparison.duration_delta_s,
                "distance_delta_m": comparison.distance_delta_m,
            }
            if comparison is not None
            else None
        ),
    }


def _entity_resolution_from_dict(
    data: dict[str, Any],
) -> EntityResolutionEvidence:
    if not isinstance(data, dict):
        raise TypeError("entity_resolution must be an object")
    comparison_data = data["comparison"]
    if comparison_data is not None and not isinstance(comparison_data, dict):
        raise TypeError("entity_resolution comparison must be an object or null")
    comparison = (
        MatchedPairEvidence(
            start_time_delta_s=comparison_data["start_time_delta_s"],
            duration_delta_s=comparison_data["duration_delta_s"],
            distance_delta_m=comparison_data["distance_delta_m"],
        )
        if comparison_data is not None
        else None
    )
    return EntityResolutionEvidence(
        rule_version=data["rule_version"],
        status=EntityResolutionStatus(data["status"]),
        source_count=data["source_count"],
        qualifying_candidate_count=data["qualifying_candidate_count"],
        counterpart_max_qualifying_candidate_count=(
            data["counterpart_max_qualifying_candidate_count"]
        ),
        comparison=comparison,
    )


def _activity_to_dict(
    activity: Activity, *, require_entity_resolution: bool
) -> dict[str, Any]:
    if require_entity_resolution and activity.entity_resolution is None:
        raise ValueError(
            "schema-v1 Activity requires entity_resolution evidence"
        )
    data: dict[str, Any] = {"schema_version": CANONICAL_SCHEMA_VERSION}
    for field in fields(activity):
        value = getattr(activity, field.name)
        if isinstance(value, MetricValue):
            data[field.name] = _metric_to_dict(value)
        elif isinstance(value, EntityResolutionEvidence):
            data[field.name] = _entity_resolution_to_dict(value)
        elif isinstance(value, (date, datetime)):
            data[field.name] = value.isoformat()
        else:
            data[field.name] = value
    return data


def activity_to_dict(activity: Activity) -> dict[str, Any]:
    return _activity_to_dict(activity, require_entity_resolution=True)


def _validate_schema_version(data: dict[str, Any]) -> None:
    if "schema_version" not in data:
        raise ValueError("missing canonical schema_version")
    version = data["schema_version"]
    if type(version) is not int:
        raise ValueError(
            "malformed canonical schema_version: expected an integer"
        )
    if version != CANONICAL_SCHEMA_VERSION:
        raise ValueError(f"unsupported canonical schema_version: {version}")


def _validate_legacy_v0(data: dict[str, Any]) -> None:
    if "schema_version" in data:
        raise ValueError("legacy v0 row must not contain schema_version")


def _parse_activity_fields(
    data: dict[str, Any],
    entity_resolution: EntityResolutionEvidence | None,
) -> Activity:
    end_time = data["end_time"]
    return Activity(
        activity_id=data["activity_id"],
        local_date=date.fromisoformat(data["local_date"]),
        start_time=datetime.fromisoformat(data["start_time"]),
        end_time=datetime.fromisoformat(end_time) if end_time else None,
        sport=data["sport"],
        sub_sport=data["sub_sport"],
        coros_label_id=data["coros_label_id"],
        coros_sport_type=data["coros_sport_type"],
        fit_file_sha256=data["fit_file_sha256"],
        entity_resolution=entity_resolution,
        **{
            name: _metric_from_dict(data[name])
            for name in ACTIVITY_METRIC_NAMES
        },
    )


def activity_from_dict(data: dict[str, Any]) -> Activity:
    _validate_schema_version(data)
    return _parse_activity_fields(
        data, _entity_resolution_from_dict(data["entity_resolution"])
    )


def _fit_projection_cache_path(
    cache_dir: Path, cache_key: _FitProjectionCacheKey
) -> Path:
    file_hash, configured_timezone, policy_version = cache_key
    variant = hashlib.sha256(
        json.dumps(
            {
                "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
                "configured_timezone": configured_timezone.key,
                "fit_projection_policy_version": policy_version,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return cache_dir / f"{file_hash}-{variant}.json"


def _fit_projection_cache_document(
    cache_key: _FitProjectionCacheKey, activity: Activity
) -> dict[str, Any]:
    file_hash, configured_timezone, policy_version = cache_key
    return {
        "cache_format_version": _FIT_PROJECTION_CACHE_FORMAT_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "fit_file_sha256": file_hash,
        "configured_timezone": configured_timezone.key,
        "fit_projection_policy_version": policy_version,
        "activity": _activity_to_dict(
            activity, require_entity_resolution=False
        ),
    }


def _read_persistent_fit_projection(
    cache_dir: Path, cache_key: _FitProjectionCacheKey
) -> Activity | None:
    path = _fit_projection_cache_path(cache_dir, cache_key)
    file_hash, configured_timezone, policy_version = cache_key
    try:
        with path.open(encoding="utf-8") as source:
            document = json.load(source)
        if document["cache_format_version"] != _FIT_PROJECTION_CACHE_FORMAT_VERSION:
            return None
        if document["canonical_schema_version"] != CANONICAL_SCHEMA_VERSION:
            return None
        if document["fit_file_sha256"] != file_hash:
            return None
        if document["configured_timezone"] != configured_timezone.key:
            return None
        if document["fit_projection_policy_version"] != policy_version:
            return None
        activity_data = document["activity"]
        _validate_schema_version(activity_data)
        if activity_data.get("entity_resolution") is not None:
            return None
        activity = _parse_activity_fields(activity_data, None)
        if activity.fit_file_sha256 != file_hash:
            return None
        return activity
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_persistent_fit_projection(
    cache_dir: Path,
    cache_key: _FitProjectionCacheKey,
    activity: Activity,
) -> None:
    path = _fit_projection_cache_path(cache_dir, cache_key)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump(
                _fit_projection_cache_document(cache_key, activity),
                output,
                sort_keys=True,
            )
            output.write("\n")
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return


def activity_from_legacy_v0_dict(data: dict[str, Any]) -> Activity:
    _validate_legacy_v0(data)
    return _parse_activity_fields(data, None)


def daily_state_to_dict(state: DailyState) -> dict[str, Any]:
    return {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "local_date": state.local_date.isoformat(),
        "device_availability": state.device_availability.value,
        **{
            field.name: _metric_to_dict(getattr(state, field.name))
            for field in fields(state)
            if isinstance(getattr(state, field.name), MetricValue)
        },
    }


def _parse_daily_state_fields(data: dict[str, Any]) -> DailyState:
    return DailyState(
        local_date=date.fromisoformat(data["local_date"]),
        device_availability=DeviceAvailability(data["device_availability"]),
        **{
            field.name: _metric_from_dict(data[field.name])
            for field in fields(DailyState)
            if field.name not in {"local_date", "device_availability"}
        },
    )


def daily_state_from_dict(data: dict[str, Any]) -> DailyState:
    _validate_schema_version(data)
    return _parse_daily_state_fields(data)


def daily_state_from_legacy_v0_dict(data: dict[str, Any]) -> DailyState:
    _validate_legacy_v0(data)
    return _parse_daily_state_fields(data)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as output:
        for row in rows:
            output.write(json.dumps(row, sort_keys=True))
            output.write("\n")
        temporary = Path(output.name)
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def _read_canonical_jsonl(
    path: Path,
    row_type: str,
    deserialize: Callable[[dict[str, Any]], _CanonicalRow],
) -> list[_CanonicalRow]:
    if not path.exists():
        raise FileNotFoundError(f"canonical JSONL file does not exist: {path}")
    rows: list[_CanonicalRow] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path}: line {line_number}: invalid JSON: {error.msg}"
                ) from error
            try:
                rows.append(deserialize(data))
            except (KeyError, TypeError, ValueError) as error:
                reason = (
                    f"missing required field: {error.args[0]}"
                    if isinstance(error, KeyError)
                    else str(error)
                )
                raise ValueError(
                    f"{path}: line {line_number}: invalid {row_type} row: "
                    f"{reason}"
                ) from error
    return rows


def read_activities_jsonl(path: Path) -> list[Activity]:
    return _read_canonical_jsonl(path, "Activity", activity_from_dict)


def read_daily_states_jsonl(path: Path) -> list[DailyState]:
    return _read_canonical_jsonl(path, "DailyState", daily_state_from_dict)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_canonical_filename(path: Path, expected: str) -> None:
    if path.name != expected:
        raise ValueError(
            f"canonical file must be named {expected!r}, got {path.name!r}"
        )


def _inventory_activities_file(
    path: Path,
) -> tuple[CanonicalFileInventory, list[Activity]]:
    _require_canonical_filename(path, CANONICAL_ACTIVITIES_FILENAME)
    activities = read_activities_jsonl(path)
    return (
        CanonicalFileInventory(
            path=CANONICAL_ACTIVITIES_FILENAME,
            row_count=len(activities),
            sha256=sha256_file(path),
        ),
        activities,
    )


def inventory_activities_file(path: Path) -> CanonicalFileInventory:
    inventory, _ = _inventory_activities_file(path)
    return inventory


def _inventory_daily_states_file(
    path: Path,
) -> tuple[CanonicalFileInventory, list[DailyState]]:
    _require_canonical_filename(path, CANONICAL_DAILY_STATES_FILENAME)
    daily_states = read_daily_states_jsonl(path)
    return (
        CanonicalFileInventory(
            path=CANONICAL_DAILY_STATES_FILENAME,
            row_count=len(daily_states),
            sha256=sha256_file(path),
        ),
        daily_states,
    )


def inventory_daily_states_file(path: Path) -> CanonicalFileInventory:
    inventory, _ = _inventory_daily_states_file(path)
    return inventory


def manifest_to_dict(manifest: CanonicalManifest) -> dict[str, object]:
    generated_at = manifest.generated_at.astimezone(UTC).isoformat()
    if not generated_at.endswith("+00:00"):
        raise ValueError("generated_at must be timezone-aware UTC")
    generated_at = f"{generated_at[:-6]}Z"
    return {
        "manifest_version": manifest.manifest_version,
        "canonical_schema_version": manifest.canonical_schema_version,
        "generator": {
            "name": manifest.generator.name,
            "version": manifest.generator.version,
        },
        "generated_at": generated_at,
        "timezone": {
            "configured": manifest.timezone.configured,
            "fit_projection_policy": manifest.timezone.fit_projection_policy,
        },
        "sources": [
            {"source": item.source.value, "included": item.included}
            for item in sorted(
                manifest.sources, key=lambda item: item.source.value
            )
        ],
        "entity_resolution": {
            "activity_rule_version": (
                manifest.entity_resolution.activity_rule_version
            )
        },
        "device_availability": {
            "unavailable_local_date_intervals": [
                {"start": item.start.isoformat(), "end": item.end.isoformat()}
                for item in sorted(
                    manifest.device_unavailable_intervals,
                    key=lambda item: (item.start, item.end),
                )
            ]
        },
        "files": {
            "activities": {
                "path": manifest.activities_file.path,
                "row_count": manifest.activities_file.row_count,
                "sha256": manifest.activities_file.sha256,
            },
            "daily_states": {
                "path": manifest.daily_states_file.path,
                "row_count": manifest.daily_states_file.row_count,
                "sha256": manifest.daily_states_file.sha256,
            },
        },
    }


def _required(data: Mapping[str, object], key: str) -> object:
    if key not in data:
        raise ValueError(f"missing required field: {key}")
    return data[key]


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _list(value: object, name: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{name} must be a JSON array")
    return value


def _parse_generated_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("generated_at must be a string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00", 1))
    except ValueError as error:
        raise ValueError(f"generated_at must be a valid datetime: {error}") from error


def _parse_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a valid ISO date: {error}") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use YYYY-MM-DD format")
    return parsed


def _file_inventory(value: object, name: str) -> CanonicalFileInventory:
    data = _mapping(value, name)
    return CanonicalFileInventory(
        path=_required(data, "path"),
        row_count=_required(data, "row_count"),
        sha256=_required(data, "sha256"),
    )


def manifest_from_dict(data: Mapping[str, object]) -> CanonicalManifest:
    if not isinstance(data, Mapping):
        raise TypeError("canonical manifest must be an object")
    manifest_version = _required(data, "manifest_version")
    if type(manifest_version) is not int:
        raise TypeError("manifest_version must be an integer")
    if manifest_version != CANONICAL_MANIFEST_VERSION:
        raise ValueError(
            f"unsupported canonical manifest version: {manifest_version}"
        )

    generator = _mapping(_required(data, "generator"), "generator")
    timezone_data = _mapping(_required(data, "timezone"), "timezone")
    source_values = _list(_required(data, "sources"), "sources")
    sources = []
    for index, value in enumerate(source_values):
        source = _mapping(value, f"sources[{index}]")
        sources.append(
            SourceInclusion(
                source=DataSource(_required(source, "source")),
                included=_required(source, "included"),
            )
        )

    resolution = _mapping(
        _required(data, "entity_resolution"), "entity_resolution"
    )
    availability = _mapping(
        _required(data, "device_availability"), "device_availability"
    )
    interval_values = _list(
        _required(availability, "unavailable_local_date_intervals"),
        "device_availability.unavailable_local_date_intervals",
    )
    intervals = []
    for index, value in enumerate(interval_values):
        interval = _mapping(
            value,
            f"device_availability.unavailable_local_date_intervals[{index}]",
        )
        intervals.append(
            ManifestDeviceUnavailableInterval(
                start=_parse_date(_required(interval, "start"), "start"),
                end=_parse_date(_required(interval, "end"), "end"),
            )
        )

    files = _mapping(_required(data, "files"), "files")
    return CanonicalManifest(
        manifest_version=manifest_version,
        canonical_schema_version=_required(data, "canonical_schema_version"),
        generator=GeneratorIdentity(
            name=_required(generator, "name"),
            version=_required(generator, "version"),
        ),
        generated_at=_parse_generated_at(_required(data, "generated_at")),
        timezone=CanonicalTimezoneConfig(
            configured=_required(timezone_data, "configured"),
            fit_projection_policy=_required(
                timezone_data, "fit_projection_policy"
            ),
        ),
        sources=tuple(sources),
        entity_resolution=EntityResolutionConfig(
            activity_rule_version=_required(
                resolution, "activity_rule_version"
            )
        ),
        device_unavailable_intervals=tuple(intervals),
        activities_file=_file_inventory(
            _required(files, "activities"), "files.activities"
        ),
        daily_states_file=_file_inventory(
            _required(files, "daily_states"), "files.daily_states"
        ),
    )


def read_canonical_manifest(path: Path) -> CanonicalManifest:
    if not path.exists():
        raise FileNotFoundError(f"canonical manifest does not exist: {path}")
    try:
        with path.open(encoding="utf-8") as source:
            data = json.load(source)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: invalid JSON: {error.msg}") from error
    try:
        return manifest_from_dict(data)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{path}: invalid canonical manifest: {error}") from error


def write_canonical_manifest(path: Path, manifest: CanonicalManifest) -> None:
    serialized = json.dumps(
        manifest_to_dict(manifest), indent=2, sort_keys=True, ensure_ascii=False
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(serialized)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _validate_file_inventory(
    role: str,
    expected: CanonicalFileInventory,
    actual: CanonicalFileInventory,
) -> None:
    if actual.row_count != expected.row_count:
        raise ValueError(
            f"{role} canonical file {expected.path!r}: row-count mismatch: "
            f"expected {expected.row_count}, actual {actual.row_count}"
        )
    if actual.sha256 != expected.sha256:
        raise ValueError(
            f"{role} canonical file {expected.path!r}: SHA-256 mismatch: "
            f"expected {expected.sha256}, actual {actual.sha256}"
        )


def validate_canonical_dataset(manifest_path: Path) -> CanonicalManifest:
    manifest = read_canonical_manifest(manifest_path)
    if manifest.canonical_schema_version != CANONICAL_SCHEMA_VERSION:
        raise ValueError(
            "manifest canonical_schema_version does not match the supported "
            "canonical schema version"
        )

    activities_path = manifest_path.parent / manifest.activities_file.path
    activities_inventory, activities = _inventory_activities_file(
        activities_path
    )
    _validate_file_inventory(
        "activities", manifest.activities_file, activities_inventory
    )
    expected_rule = manifest.entity_resolution.activity_rule_version
    for row_number, activity in enumerate(activities, start=1):
        resolution = activity.entity_resolution
        if resolution is None or resolution.rule_version != expected_rule:
            actual_rule = (
                resolution.rule_version if resolution is not None else None
            )
            raise ValueError(
                f"activities canonical file {manifest.activities_file.path!r}: "
                f"Activity row {row_number} entity-resolution rule mismatch: "
                f"expected {expected_rule!r}, actual {actual_rule!r}"
            )

    daily_states_path = manifest_path.parent / manifest.daily_states_file.path
    daily_states_inventory, _ = _inventory_daily_states_file(
        daily_states_path
    )
    _validate_file_inventory(
        "daily states", manifest.daily_states_file, daily_states_inventory
    )
    return manifest


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def resolve_generator_identity() -> GeneratorIdentity:
    distribution_name = "coros-analyzer"
    try:
        distribution_version = importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeError(
            f"installed distribution metadata not found for {distribution_name!r}"
        ) from error
    return GeneratorIdentity(distribution_name, distribution_version)


def build_canonical_dataset(
    stage1_dir: Path,
    fit_dir: Path,
    configured_timezone_name: str,
    device_unavailable_intervals: Sequence[DeviceUnavailableInterval] = (),
    generated_at: datetime | None = None,
) -> dict[str, int | float]:
    configured_timezone = ZoneInfo(configured_timezone_name)
    generator = resolve_generator_identity()
    manifest_intervals = tuple(
        ManifestDeviceUnavailableInterval(start, end)
        for start, end in device_unavailable_intervals
    )
    timings: dict[str, float] = {}
    coros_rows = _read_csv(stage1_dir / "activities.csv")
    daily_rows = _read_csv(stage1_dir / "daily_recovery.csv")
    bindings = bootstrap_legacy_managed_bindings(fit_dir, coros_rows)
    fit_discovery_started = perf_counter()
    fit_files = sorted(fit_dir.rglob("*.fit")) if fit_dir.exists() else []
    timings["fit_discovery_seconds"] = perf_counter() - fit_discovery_started
    activities = build_canonical_activities(
        fit_files,
        coros_rows,
        configured_timezone,
        timings,
        fit_dir / _FIT_PROJECTION_CACHE_RELATIVE_PATH,
        bindings,
    )
    daily_states = sorted(
        (
            project_daily_state(row, device_unavailable_intervals)
            for row in daily_rows
        ),
        key=lambda state: state.local_date,
    )
    activities_path = stage1_dir / CANONICAL_ACTIVITIES_FILENAME
    daily_states_path = stage1_dir / CANONICAL_DAILY_STATES_FILENAME
    manifest_path = stage1_dir / CANONICAL_MANIFEST_FILENAME
    serialization_started = perf_counter()
    manifest_path.unlink(missing_ok=True)
    write_jsonl(
        activities_path,
        [activity_to_dict(activity) for activity in activities],
    )
    write_jsonl(
        daily_states_path,
        [daily_state_to_dict(state) for state in daily_states],
    )
    serialization_seconds = perf_counter() - serialization_started
    inventory_started = perf_counter()
    activities_inventory = inventory_activities_file(activities_path)
    daily_states_inventory = inventory_daily_states_file(daily_states_path)
    timings["inventory_seconds"] = perf_counter() - inventory_started
    manifest = CanonicalManifest(
        manifest_version=CANONICAL_MANIFEST_VERSION,
        canonical_schema_version=CANONICAL_SCHEMA_VERSION,
        generator=generator,
        generated_at=(
            datetime.now(UTC) if generated_at is None else generated_at
        ),
        timezone=CanonicalTimezoneConfig(
            configured=configured_timezone_name,
            fit_projection_policy=FIT_PROJECTION_POLICY_VERSION,
        ),
        sources=(
            SourceInclusion(DataSource.COROS_MCP, True),
            SourceInclusion(DataSource.FIT, True),
        ),
        entity_resolution=EntityResolutionConfig(
            activity_rule_version=ACTIVITY_MATCH_RULE_VERSION
        ),
        device_unavailable_intervals=manifest_intervals,
        activities_file=activities_inventory,
        daily_states_file=daily_states_inventory,
    )
    serialization_started = perf_counter()
    write_canonical_manifest(manifest_path, manifest)
    timings["serialization_seconds"] = (
        serialization_seconds + perf_counter() - serialization_started
    )
    return {
        "canonical_activities_rows": len(activities),
        "canonical_daily_states_rows": len(daily_states),
        "canonical_unmatched_fit_activities": sum(
            activity.coros_label_id is None and activity.fit_file_sha256 is not None
            for activity in activities
        ),
        **timings,
    }
