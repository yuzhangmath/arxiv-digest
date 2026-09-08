# Hostile update-wheel fixtures

The tests build hostile wheels in memory in
`tests/unit/test_update_manifest.py`; it does not commit opaque wheel binaries.
The tests start from one deterministic, valid `py3-none-any` wheel and make one
reviewable mutation per case.

The generated cases cover:

- unsafe, duplicate, non-regular, and encrypted members;
- malformed EOCD and central-directory bounds, multidisk ZIPs, ZIP64 markers,
  inconsistent local/central flags, and excessive compression ratios;
- missing, oversized, malformed, or ambiguous `METADATA`, `WHEEL`, and `RECORD`
  members;
- non-universal tags, non-purelib wheels, unsupported wheel metadata, invalid
  distribution/version/filename identities, and unsupported Python policies;
- incomplete or duplicate `RECORD` paths, invalid digest syntax, non-empty
  `RECORD` self entries, and mismatched bounded-member hashes or member sizes;
- symlink wheel paths, size checks before archive parsing, bounded reads, and
  in-place file changes during inspection.

Keeping the mutation logic next to each assertion makes the hostile property
explicit and prevents fixture regeneration tools from silently normalizing the
malformed archive.
