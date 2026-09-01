from __future__ import annotations

import re


def normalized_sport_family(value: str | None) -> str:
    """Return the canonical sport family; missing or blank values are unknown."""
    normalized = re.sub(r"[^a-z0-9]+", "", (value or "").lower())
    if normalized in {
        "outdoorrun",
        "run",
        "running",
        "trailrun",
        "trailrunning",
    }:
        return "running"
    if normalized in {"bike", "biking", "cycle", "cycling", "roadcycling"}:
        return "cycling"
    if normalized in {"hike", "hiking"}:
        return "hiking"
    return normalized or "unknown"
