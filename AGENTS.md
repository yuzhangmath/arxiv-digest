# arXiv Digest guidance

## Project map

- Use [README.md](README.md) for supported user behavior and
  [CONTRIBUTING.md](CONTRIBUTING.md) for development and release checks.
- Python application code is in `src/arxiv_digest/`; browser assets are in
  `src/arxiv_digest/web/static/`. Tests are divided into `unit`, `integration`,
  `js`, and `browser` under `tests/`.
- Preserve the local, explainable ranking and review workflow. Review/Calendar
  membership comes from recovered daily lists; Library membership is a
  separate explicit save action. Read the relevant README section before
  changing those semantics.

## User data and storage

- Profiles, library/review state, downloaded PDFs, and portable backups are
  user data. Use the existing test isolation and synthetic fixtures for
  development; do not use the installed application's live state as a fixture.
- Before changing persistence or restore behavior, read
  [Data and backup](docs/data-and-backup.md) and the existing storage migrations.
  Respect this application's generation, schema, restore, and recovery rules;
  its durable data is not a disposable computation output.
- Preserve the documented separation of durable data, regenerable cache, PDF
  files, and backup contents. Keep personal collections and identifying data
  out of fixtures, reports, and release artifacts as required by CONTRIBUTING.

## Validation

- Use the existing `.venv` and the Python/Node versions specified in
  CONTRIBUTING. Start with the relevant test file; the suite entry points are:

  ```sh
  .venv/bin/python -m pytest tests/unit tests/integration -q
  node --test tests/js/*.test.mjs
  .venv/bin/python -m pytest tests/browser -q
  ```

- Application tests block non-loopback network access. Preserve deterministic
  local fixtures rather than fetching live arXiv responses in tests.
- Browser changes require Chromium and WebKit coverage. For packaging or
  release work, follow the existing package inventory and privacy checks in
  CONTRIBUTING and `.github/workflows/`; choose the checks relevant to the
  changed surface without duplicating their definitions here.
