import json

import summarize
from conftest import REPO_DIR


def test_chart_data_is_current():
    assert summarize.main(["--check"]) == 0


def test_manifests_hold_no_absolute_paths():
    for path in sorted((REPO_DIR / "results" / "manifests").glob("*.json")):
        manifest = json.loads(path.read_text())
        assert not manifest["job_file"].startswith("/"), path.name
        assert (REPO_DIR / manifest["job_file"]).exists(), path.name
        assert "/home/" not in path.read_text(), path.name


def test_every_video_clip_has_the_same_shape():
    rows = summarize.video_rows(summarize.load_manifests())
    assert len(rows) == 11
    assert {row["clip"] for row in rows} == {"5 s, 720x1280"}
