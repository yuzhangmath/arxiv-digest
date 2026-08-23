# Contributing

Thank you for helping improve arXiv Digest. Keep fixtures synthetic: never add
personal paper collections, profiles, backups, messages, credentials, local
paths, or identifying Git metadata to a contribution.

## Development setup

Use Python 3.11 or a newer supported Python and Node.js 24.19.0:

```bash
python -m venv .venv
.venv/bin/python -m pip install ".[dev]"
```

Application tests deny non-loopback network access. Add deterministic local
fixtures for source behavior instead of contacting arXiv.

## Test changes

Run the smallest relevant test while developing, then the offline suites:

```bash
.venv/bin/python -m build
.venv/bin/python -m pytest tests/unit tests/integration -q
node --test tests/js/*.test.mjs
.venv/bin/python -m playwright install chromium webkit
.venv/bin/python -m pytest tests/browser -q
.venv/bin/python -m pytest tests/integration/test_package_contents.py -q
.venv/bin/python scripts/privacy_scan.py tree . --expect-no-remote
.venv/bin/python scripts/privacy_scan.py archive dist/arxiv_digest-0.1.0.tar.gz
.venv/bin/python scripts/privacy_scan.py archive dist/arxiv_digest-0.1.0-py3-none-any.whl
```

Browser changes must pass both Chromium and WebKit. Packaging changes must
preserve the exact migration, static-asset, license, and notice inventories.

## Privacy review

The generic tree and archive scans are necessary but do not replace the
private-derived release audit performed by the maintainer. Scanner diagnostics
must name only a relative path, artifact class, and opaque rule identifier;
they must never echo a matched value. Do not weaken a rule to accommodate a
real private artifact. Rewrite synthetic test data so no realistic private
value is stored in the repository.

## Pull requests

Explain observable behavior, tests run, platform-specific implications, and
any remaining limitation. Keep changes focused. Do not add telemetry,
background scheduling, remote accounts, deployment credentials, or live-network
tests without an approved design and explicit review.
