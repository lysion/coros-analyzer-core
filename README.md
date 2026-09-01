# coros-analyzer-core

`coros-analyzer-core` canonicalizes sports activity data into stable, typed
models while preserving where each value came from and what is missing. It
provides FIT data utilities and conservative COROS/FIT activity identity
binding for applications that need a trustworthy dataset boundary before
analysis, storage, or visualization.

Unlike a FIT parser alone, this library keeps metric provenance and explicit
missingness semantics, produces stable canonical models, and validates
canonical snapshots reproducibly with a manifest and file fingerprints.

## Features

- Provenance-preserving `Activity`, `DailyState`, and `MetricValue` models.
- Explicit distinction between present, absent, and source-null metric values.
- FIT projection helpers built on `fitdecode`.
- Conservative COROS/FIT identity binding and canonical JSONL construction.
- Versioned manifests with row counts and SHA-256 validation.
- A deliberately small public Python API: `coros_analyzer.__all__`.

## Typical data flow

```text
FIT files / prepared activity data
        ↓
coros-analyzer-core
        ↓
canonical models + validated JSONL snapshot
        ↓
downstream analysis, database, or visualization
```

## Quick Start

Install from a source checkout:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

Create a metric with explicit source, origin, unit, and availability:

```python
from coros_analyzer import (
    DataAvailability,
    DataSource,
    MetricOrigin,
    MetricValue,
)

distance_m = MetricValue(
    value=5000.0,
    source=DataSource.FIT,
    origin=MetricOrigin.SOURCE_SUMMARY,
    unit="m",
)

assert distance_m.availability is DataAvailability.PRESENT
print(distance_m.value, distance_m.unit)  # 5000.0 m
```

### Reading an existing canonical snapshot

If you already have a canonical snapshot produced by a compatible ingestion
pipeline, validate its manifest before reading its rows:

```python
from pathlib import Path

from coros_analyzer import (
    read_activities_jsonl,
    read_daily_states_jsonl,
    validate_canonical_dataset,
)

dataset_dir = Path("canonical-output")
manifest = validate_canonical_dataset(
    dataset_dir / "canonical_manifest.json"
)
activities = read_activities_jsonl(dataset_dir / manifest.activities_file.path)
daily_states = read_daily_states_jsonl(
    dataset_dir / manifest.daily_states_file.path
)

print(len(activities), len(daily_states))
```

`build_canonical_dataset` is also part of the public API for applications that
already prepare compatible local activity and daily-state inputs. It performs
local canonical construction only; this package does not provide COROS sync,
OAuth, a CLI, reports, or training analysis.

## Scope

This package is the public canonical-data core. It includes data models, FIT
utilities, identity binding, canonical JSONL construction, and validation. It
does not include COROS MCP acquisition, OAuth, backfill or hydration workflows,
personal reports, interpretation, training review, or Agent Skills.

## Data Safety

Do not commit activity exports, FIT/GPX/TCX files, canonical datasets, OAuth
material, credentials, or report output. Tests use constructed synthetic
values. Ignore rules reduce accidental additions but do not replace a
release-time sensitive-data scan.

## License

Licensed under Apache-2.0. See [LICENSE](LICENSE).

See [RELEASE_BLOCKERS.md](RELEASE_BLOCKERS.md) for release-scope notes.
