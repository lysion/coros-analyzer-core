from __future__ import annotations

import warnings

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import fitdecode


@dataclass
class FitField:
    name: str
    value: Any
    units: str | None


@dataclass
class FitMessage:
    name: str
    fields: list[FitField]


@dataclass(frozen=True)
class FitDecodeDiagnostics:
    decode_seconds: float
    file_size_bytes: int
    message_count: int
    record_count: int
    session_count: int
    lap_count: int


class FitDataSource:
    def read(self, fit_file: Path) -> list[FitMessage]:
        messages: list[FitMessage] = []

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=(
                    r"^invalid field size 1 in definition message @ \d+ for "
                    r"type uint32 \(expected a multiple of 4\)$"
                ),
                category=UserWarning,
                module=r"^fitdecode\.reader$",
            )
            with fitdecode.FitReader(fit_file) as reader:
                for frame in reader:
                    if not isinstance(frame, fitdecode.FitDataMessage):
                        continue

                    fields = [
                        FitField(
                            name=field.name,
                            value=field.value,
                            units=field.units,
                        )
                        for field in frame.fields
                    ]
                    messages.append(
                        FitMessage(name=frame.name, fields=fields)
                    )

        return messages

    def read_with_diagnostics(
        self, fit_file: Path
    ) -> tuple[list[FitMessage], FitDecodeDiagnostics]:
        file_size_bytes = fit_file.stat().st_size
        started = perf_counter()
        messages = self.read(fit_file)
        decode_seconds = perf_counter() - started
        message_counts = {
            name: sum(message.name == name for message in messages)
            for name in ("record", "session", "lap")
        }
        return messages, FitDecodeDiagnostics(
            decode_seconds=decode_seconds,
            file_size_bytes=file_size_bytes,
            message_count=len(messages),
            record_count=message_counts["record"],
            session_count=message_counts["session"],
            lap_count=message_counts["lap"],
        )
