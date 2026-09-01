from __future__ import annotations

import hashlib
import json
import os
import re

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile


ACQUISITION_BINDING_SCHEMA_VERSION = 1
ACQUISITION_BINDINGS_FILENAME = "acquisition_bindings.jsonl"
EXACT_DOWNLOAD_ORIGIN = "coros_mcp_exact_download"
LEGACY_FILENAME_ORIGIN = "legacy_managed_filename"
_VALID_ORIGINS = {EXACT_DOWNLOAD_ORIGIN, LEGACY_FILENAME_ORIGIN}
_SHA_PATTERN = re.compile(r"[0-9a-f]{64}")
_LEGACY_FILENAME_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}-(?P<label_id>.+)-(?P<sport_type>\d+)\.fit"
)


@dataclass(frozen=True)
class FitAcquisitionBinding:
    fit_file_sha256: str
    coros_label_id: str
    coros_sport_type: int
    binding_origin: str

    def __post_init__(self) -> None:
        if _SHA_PATTERN.fullmatch(self.fit_file_sha256) is None:
            raise ValueError("fit_file_sha256 must be 64 lowercase hex characters")
        if not isinstance(self.coros_label_id, str) or not self.coros_label_id:
            raise ValueError("coros_label_id must be a non-empty string")
        if type(self.coros_sport_type) is not int:
            raise TypeError("coros_sport_type must be an integer")
        if self.binding_origin not in _VALID_ORIGINS:
            raise ValueError(f"unsupported binding_origin: {self.binding_origin!r}")

    @property
    def coros_identity(self) -> tuple[str, int]:
        return self.coros_label_id, self.coros_sport_type


def acquisition_bindings_path(fit_dir: Path) -> Path:
    return fit_dir / "coros-mcp" / ACQUISITION_BINDINGS_FILENAME


def _binding_from_mapping(row: Mapping[str, object]) -> FitAcquisitionBinding:
    expected = {
        "schema_version",
        "fit_file_sha256",
        "coros_label_id",
        "coros_sport_type",
        "binding_origin",
    }
    if set(row) != expected:
        raise ValueError("acquisition binding row has unexpected fields")
    if row["schema_version"] != ACQUISITION_BINDING_SCHEMA_VERSION:
        raise ValueError("unsupported acquisition binding schema_version")
    return FitAcquisitionBinding(
        fit_file_sha256=row["fit_file_sha256"],  # type: ignore[arg-type]
        coros_label_id=row["coros_label_id"],  # type: ignore[arg-type]
        coros_sport_type=row["coros_sport_type"],  # type: ignore[arg-type]
        binding_origin=row["binding_origin"],  # type: ignore[arg-type]
    )


def _merge_bindings(
    bindings: Iterable[FitAcquisitionBinding],
) -> tuple[FitAcquisitionBinding, ...]:
    by_sha: dict[str, FitAcquisitionBinding] = {}
    by_identity: dict[tuple[str, int], FitAcquisitionBinding] = {}
    for binding in bindings:
        sha_existing = by_sha.get(binding.fit_file_sha256)
        if (
            sha_existing is not None
            and sha_existing.coros_identity != binding.coros_identity
        ):
            raise ValueError("one FIT SHA is bound to multiple COROS identities")
        identity_existing = by_identity.get(binding.coros_identity)
        if (
            identity_existing is not None
            and identity_existing.fit_file_sha256 != binding.fit_file_sha256
        ):
            raise ValueError("one COROS identity is bound to multiple FIT SHAs")
        existing = sha_existing or identity_existing
        if existing is not None:
            if binding.binding_origin == EXACT_DOWNLOAD_ORIGIN:
                by_sha[binding.fit_file_sha256] = binding
                by_identity[binding.coros_identity] = binding
            continue
        by_sha[binding.fit_file_sha256] = binding
        by_identity[binding.coros_identity] = binding
    return tuple(
        sorted(
            by_sha.values(),
            key=lambda item: (
                item.coros_label_id,
                item.coros_sport_type,
                item.fit_file_sha256,
            ),
        )
    )


def load_acquisition_bindings(
    fit_dir: Path,
) -> tuple[FitAcquisitionBinding, ...]:
    path = acquisition_bindings_path(fit_dir)
    if not path.exists():
        return ()
    bindings: list[FitAcquisitionBinding] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                document = json.loads(line)
                if not isinstance(document, dict):
                    raise ValueError("row must be a JSON object")
                bindings.append(_binding_from_mapping(document))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(
                    f"{path}: invalid acquisition binding row {line_number}: {error}"
                ) from error
    return _merge_bindings(bindings)


def write_acquisition_bindings(
    fit_dir: Path, bindings: Iterable[FitAcquisitionBinding]
) -> None:
    rows = _merge_bindings(bindings)
    path = acquisition_bindings_path(fit_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            for binding in rows:
                document = {
                    "schema_version": ACQUISITION_BINDING_SCHEMA_VERSION,
                    "fit_file_sha256": binding.fit_file_sha256,
                    "coros_label_id": binding.coros_label_id,
                    "coros_sport_type": binding.coros_sport_type,
                    "binding_origin": binding.binding_origin,
                }
                temporary.write(json.dumps(document, sort_keys=True) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def upsert_acquisition_binding(
    fit_dir: Path, binding: FitAcquisitionBinding
) -> tuple[FitAcquisitionBinding, ...]:
    bindings = _merge_bindings((*load_acquisition_bindings(fit_dir), binding))
    write_acquisition_bindings(fit_dir, bindings)
    return bindings


def bootstrap_legacy_managed_bindings(
    fit_dir: Path, coros_rows: Iterable[Mapping[str, object]]
) -> tuple[FitAcquisitionBinding, ...]:
    identities = {
        (str(row["label_id"]), int(row["sport_type"])) for row in coros_rows
    }
    bindings = list(load_acquisition_bindings(fit_dir))
    managed_dir = fit_dir / "coros-mcp"
    if managed_dir.exists():
        for path in sorted(managed_dir.glob("*.fit")):
            match = _LEGACY_FILENAME_PATTERN.fullmatch(path.name)
            if match is None:
                continue
            try:
                date.fromisoformat(path.name[:10])
            except ValueError:
                continue
            identity = (match["label_id"], int(match["sport_type"]))
            if identity not in identities:
                continue
            bindings.append(
                FitAcquisitionBinding(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    identity[0],
                    identity[1],
                    LEGACY_FILENAME_ORIGIN,
                )
            )
    merged = _merge_bindings(bindings)
    if merged != tuple(load_acquisition_bindings(fit_dir)):
        write_acquisition_bindings(fit_dir, merged)
    return merged


__all__ = [
    "ACQUISITION_BINDING_SCHEMA_VERSION",
    "FitAcquisitionBinding",
    "acquisition_bindings_path",
    "bootstrap_legacy_managed_bindings",
    "load_acquisition_bindings",
    "upsert_acquisition_binding",
]
