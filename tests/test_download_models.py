"""bootstrap/download_models.py with a fake Hugging Face fetch and metadata lookup."""

from __future__ import annotations

import hashlib
import json

import pytest

import download_models
from download_models import download_file

GOOD = b"weights" * 100
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()
COMMIT = "a" * 40
ENTRY = {"repo": "org/models", "path": "split_files/vae/vae.safetensors", "dest": "vae"}


class FakeHub:
    def __init__(self, body: bytes = GOOD, size: int | None = None, sha256: str | None = GOOD_SHA):
        self.body = body
        self.meta = {"size": len(GOOD) if size is None else size, "sha256": sha256, "commit": COMMIT}
        self.fetches: list[dict] = []

    def fetch(self, repo_id, filename, local_dir, revision=None):
        self.fetches.append({"repo": repo_id, "path": filename, "revision": revision})
        target = local_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.body)
        return str(target)

    def metadata(self, _entry):
        return self.meta


def run_download(hub: FakeHub, tmp_path, entry=ENTRY) -> dict:
    return download_file(entry, tmp_path, tmp_path / "_staging", fetch=hub.fetch, metadata=hub.metadata)


def test_download_moves_the_verified_file_into_place(tmp_path):
    hub = FakeHub()
    record = run_download(hub, tmp_path)
    assert (tmp_path / "vae" / "vae.safetensors").read_bytes() == GOOD
    assert record == {
        "repo": "org/models",
        "path": ENTRY["path"],
        "revision": COMMIT,
        "size": len(GOOD),
        "sha256": GOOD_SHA,
    }


def test_pinned_revision_is_passed_to_the_fetch(tmp_path):
    hub = FakeHub()
    run_download(hub, tmp_path, {**ENTRY, "revision": COMMIT})
    assert hub.fetches[0]["revision"] == COMMIT


def test_cached_file_of_the_right_size_is_not_downloaded_again(tmp_path):
    (tmp_path / "vae").mkdir()
    (tmp_path / "vae" / "vae.safetensors").write_bytes(GOOD)
    hub = FakeHub()
    run_download(hub, tmp_path)
    assert hub.fetches == []


def test_truncated_cached_file_is_downloaded_again(tmp_path, capsys):
    (tmp_path / "vae").mkdir()
    (tmp_path / "vae" / "vae.safetensors").write_bytes(GOOD[:10])
    hub = FakeHub()
    run_download(hub, tmp_path)
    assert len(hub.fetches) == 1
    assert (tmp_path / "vae" / "vae.safetensors").read_bytes() == GOOD
    assert "failed verification" in capsys.readouterr().out


def test_cached_file_that_does_not_match_a_pinned_sha256_is_downloaded_again(tmp_path):
    (tmp_path / "vae").mkdir()
    (tmp_path / "vae" / "vae.safetensors").write_bytes(b"x" * len(GOOD))
    hub = FakeHub()
    run_download(hub, tmp_path, {**ENTRY, "sha256": GOOD_SHA})
    assert len(hub.fetches) == 1


def test_corrupt_download_is_rejected_and_not_moved_into_place(tmp_path):
    hub = FakeHub(body=b"y" * len(GOOD))
    with pytest.raises(RuntimeError, match="sha256"):
        run_download(hub, tmp_path)
    assert not (tmp_path / "vae" / "vae.safetensors").exists()


def test_short_download_is_rejected(tmp_path):
    hub = FakeHub(body=GOOD[:10], sha256=None)
    with pytest.raises(RuntimeError, match="size"):
        run_download(hub, tmp_path)


def test_main_records_the_lock_file_and_exits_nonzero_when_every_file_fails(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "models.json"
    manifest.write_text(json.dumps({"vae": {"files": [ENTRY]}}))
    hub = FakeHub()
    monkeypatch.setattr(download_models, "hf_fetch", hub.fetch)
    monkeypatch.setattr(download_models, "remote_metadata", hub.metadata)
    argv = ["--manifest", str(manifest), "--groups", "vae", "--dest", str(tmp_path / "models")]
    download_models.main(argv)
    lock = json.loads((tmp_path / "models" / "models.lock.json").read_text())
    assert lock["org/models::split_files/vae/vae.safetensors"]["revision"] == COMMIT

    hub.meta = {**hub.meta, "size": 1}
    (tmp_path / "models" / "vae" / "vae.safetensors").unlink()
    with pytest.raises(SystemExit) as exit_info:
        download_models.main(argv)
    assert exit_info.value.code == 1
    assert "1 failed" in capsys.readouterr().out
