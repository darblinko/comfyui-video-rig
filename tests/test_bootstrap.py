"""Static and functional checks on bootstrap/bootstrap.sh.

The script needs root, apt and systemd, so it is not run end to end. The
install_repo function is extracted and run against local git repositories."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from conftest import REPO_DIR

BOOTSTRAP = (REPO_DIR / "bootstrap" / "bootstrap.sh").read_text()


def git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def step_index(marker: str) -> int:
    index = BOOTSTRAP.find(marker)
    assert index >= 0, marker
    return index


def test_watchdog_is_enabled_before_anything_slow_or_failure_prone():
    watchdog = step_index("systemctl enable --now rig-idle-watchdog.timer")
    for later in (
        "install_repo https://github.com/comfyanonymous/ComfyUI",
        "download_models.py",
        "$TORCH_REQUIREMENTS",
    ):
        assert watchdog < step_index(later)


def test_every_repository_goes_through_install_repo_with_a_ref():
    assert "git clone" not in BOOTSTRAP.replace('    git clone "$url" "$dir"', "")
    for variable in ("COMFYUI_REF", "COMFYUI_MANAGER_REF", "VIDEOHELPERSUITE_REF", "TORCH_REQUIREMENTS"):
        assert re.search(rf"^{variable}=", (REPO_DIR / "bootstrap" / "versions.env").read_text(), re.M)
        assert f"${variable}" in BOOTSTRAP


def test_a_failed_model_download_does_not_stop_the_services():
    download = step_index("download_models.py")
    assert BOOTSTRAP.rfind("if ! ", 0, download) > BOOTSTRAP.rfind("\nfi\n", 0, download)
    assert step_index("systemctl enable --now comfyui.service") > download


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    commits = []
    for n in (1, 2):
        (repo / "file.txt").write_text(str(n))
        git("add", "file.txt", cwd=repo)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", f"c{n}", cwd=repo)
        commits.append(git("rev-parse", "HEAD", cwd=repo))
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=repo)
    return repo, commits


def run_install_repo(tmp_path, url, dest, ref) -> subprocess.CompletedProcess:
    function = re.search(r"^install_repo\(\) \{.*?^\}", BOOTSTRAP, re.S | re.M).group(0)
    script = f'set -euo pipefail\nVERSIONS_FILE="{tmp_path}/versions.txt"\n{function}\ninstall_repo "$1" "$2" "$3"\n'
    return subprocess.run(["bash", "-c", script, "bash", url, str(dest), ref], capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_install_repo_checks_out_the_pinned_commit_and_records_it(tmp_path, upstream):
    repo, (first, _second) = upstream
    proc = run_install_repo(tmp_path, str(repo), tmp_path / "checkout", first)
    assert proc.returncode == 0, proc.stderr
    assert git("rev-parse", "HEAD", cwd=tmp_path / "checkout") == first
    assert (tmp_path / "versions.txt").read_text() == f"{repo} {first} (pinned {first})\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_install_repo_without_a_ref_records_the_head_it_installed(tmp_path, upstream):
    repo, (_first, second) = upstream
    assert run_install_repo(tmp_path, str(repo), tmp_path / "checkout", "").returncode == 0
    assert (tmp_path / "versions.txt").read_text() == f"{repo} {second}\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_install_repo_fails_on_an_unknown_ref(tmp_path, upstream):
    repo, _ = upstream
    proc = run_install_repo(tmp_path, str(repo), tmp_path / "checkout", "0" * 40)
    assert proc.returncode != 0
