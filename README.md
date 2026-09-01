# coros-analyzer core

This repository is the clean public v0.1 staging snapshot of the
`coros-analyzer` canonical-data core. It provides provenance-preserving models,
FIT projection helpers, conservative COROS/FIT identity binding, canonical
JSONL construction, and manifest validation.

It deliberately does not include the private product CLI, COROS OAuth or MCP
acquisition, activity hydration/backfill, personal reports, interpretation,
training review, Skills, or any personal activity data.

## Install and test

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
```

The stable import surface is `coros_analyzer.__all__`. This staging package has
no installed command-line entry point. `build_canonical_dataset` is an advanced
library API that expects locally prepared, non-sensitive source inputs.

## Data safety

Do not commit activity exports, FIT/GPX/TCX files, canonical datasets, OAuth
material, credentials, or report output. Tests use only constructed synthetic
values. The ignore rules reduce accidental additions but do not replace a
release-time sensitive-data scan.

## License

The project is licensed under Apache-2.0; see [LICENSE](LICENSE).

See [RELEASE_BLOCKERS.md](RELEASE_BLOCKERS.md) for known pre-release work.
