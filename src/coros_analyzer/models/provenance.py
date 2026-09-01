from __future__ import annotations

from enum import Enum
from dataclasses import dataclass
from typing import Generic, TypeVar

class DataSource(str, Enum):
    COROS_MCP = "coros_mcp"
    FIT = "fit"


class DataAvailability(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    NULL = "null"


class MetricOrigin(str, Enum):
    RAW = "raw"
    SOURCE_SUMMARY = "source_summary"
    COROS_VENDOR = "coros_vendor"
    DERIVED = "derived"

T = TypeVar("T")

@dataclass(frozen=True)
class MetricValue(Generic[T]):
    value: T | None
    source: DataSource
    origin: MetricOrigin
    unit: str | None = None
    availability: DataAvailability = DataAvailability.PRESENT

    def __post_init__(self) -> None:
        if self.availability is DataAvailability.PRESENT and self.value is None:
            raise ValueError("present metric value must not be None")
        if (
            self.availability is not DataAvailability.PRESENT
            and self.value is not None
        ):
            raise ValueError("unavailable metric value must be None")
