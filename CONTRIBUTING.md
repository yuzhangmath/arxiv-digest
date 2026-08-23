# Contributing

Thank you for helping improve arXiv Digest. Keep fixtures synthetic: never add
personal paper collections, profiles, backups, messages, credentials, local
paths, or identifying Git metadata to a contribution.

## Development setup

Open a terminal in the repository root. Use Python 3.11 or a newer supported
Python and Node.js 24.19.0 to create the development environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install --editable ".[dev]"
```

Application tests deny non-loopback network access. Add deterministic local
fixtures for source behavior instead of contacting arXiv.

## Test changes

From the repository root, run the smallest relevant test while developing,
then run the offline suites below one command at a time:

```bash
.venv/bin/python -m build
.venv/bin/python -m pytest tests/unit tests/integration -q
node --test tests/js/*.test.mjs
.venv/bin/python -m playwright install chromium webkit
.venv/bin/python -m pytest tests/browser -q
.venv/bin/python -m pytest tests/integration/test_package_contents.py -q
.venv/bin/python scripts/privacy_scan.py tree . --expected-remote-from-project
.venv/bin/python scripts/privacy_scan.py archive dist/arxiv_digest-0.1.0.tar.gz
.venv/bin/python scripts/privacy_scan.py archive dist/arxiv_digest-0.1.0-py3-none-any.whl
```

The tree command above assumes that the checkout's sole `origin` is the
maintainer SSH URL `git@github.com:yuzhangmath/arxiv-digest.git`. For a
canonical public HTTPS clone, replace `--expected-remote-from-project` with
`--expected-remote` followed by the exact canonical URL reported for `origin`
(usually `https://github.com/yuzhangmath/arxiv-digest.git`; omit `.git` only
when the recorded URL omits it). Reserve `--expect-no-remote` for an
intentional release-audit copy with no Git remote; it is not the command for an
ordinary clone.

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
