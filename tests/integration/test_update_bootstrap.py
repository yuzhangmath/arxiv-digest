from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.unit.test_backup import initialized_paths
from tests.update_release_factory import CANONICAL_GIT, real_release_installation


def test_real_021_tag_source_manually_bootstraps_030_without_resetting_data(tmp_path):
    fixture = real_release_installation(tmp_path / "pipx-fixture")
    fixture.add_source_tag("0.2.1", source_ref="v0.2.1")
    fixture.bootstrap("0.2.1")
    paths = initialized_paths(tmp_path / "app-state")
    environment = {**fixture.environ, "ARXIV_DIGEST_TESTING": "1",
                   "ARXIV_DIGEST_TEST_ROOT": str(paths.data_dir.parent)}
    old = fixture.run((fixture.exposed, "doctor"), environ=environment)
    assert old.stdout.splitlines()[0] == b"arXiv Digest 0.2.1"
    source_interpreter = (fixture.venv / "bin/python").resolve()
    before_python = source_interpreter.stat()
    durable = [paths.profile_path, paths.database_path]
    pdf_root = tmp_path / "app-state" / "Private PDF Destination"
    durable.extend(path for path in pdf_root.rglob("*") if path.is_file())
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in durable}
    commit = fixture.add_source_tag("0.3.0")
    fixture.bootstrap("0.3.0", force=True)
    new = fixture.run((fixture.exposed, "doctor"), environ=environment)
    assert new.stdout.splitlines()[0] == b"arXiv Digest 0.3.0"
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in durable} == before
    after_python = source_interpreter.stat()
    assert (after_python.st_dev, after_python.st_ino) == (before_python.st_dev, before_python.st_ino)
    assert not paths.update_provenance_path.exists()
    assert not paths.update_journal_path.exists()
    result = fixture.run((fixture.venv / "bin/python", "-c",
        "from importlib.metadata import distribution; print(distribution('arxiv-digest').read_text('direct_url.json'))"))
    origin = json.loads(result.stdout)
    assert origin == {"url": CANONICAL_GIT, "vcs_info": {"vcs": "git", "requested_revision": "v0.3.0", "commit_id": commit}}
    pipx = json.loads((fixture.venv / "pipx_metadata.json").read_bytes())
    assert pipx["main_package"]["package_or_url"] == f"git+{CANONICAL_GIT}@v0.3.0"
    assert pipx["main_package"]["pip_args"] == []
