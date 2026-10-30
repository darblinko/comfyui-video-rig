"""Runs bootstrap/idle_watchdog.sh with stub nvidia-smi, curl and shutdown commands."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_DIR

WATCHDOG = REPO_DIR / "bootstrap" / "idle_watchdog.sh"
STUBS = {
    "nvidia-smi": 'echo "${STUB_GPU_UTIL:-0}"',
    "curl": 'if [ -n "${STUB_QUEUE_JSON:-}" ]; then echo "$STUB_QUEUE_JSON"; else exit 7; fi',
    "shutdown": 'echo "$*" >> "$STUB_SHUTDOWN_LOG"',
}

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


class Box:
    """A fake instance: temp state dir, env file, uptime file and stub commands."""

    def __init__(self, root: Path):
        self.root = root
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in STUBS.items():
            stub = bin_dir / name
            stub.write_text(f"#!/bin/bash\n{body}\n")
            stub.chmod(0o755)
        (root / "rig.env").write_text("RIG_IDLE_STOP_MINUTES=30\nRIG_MAX_JOB_MINUTES=360\nRIG_COMFY_PORT=8188\n")
        self.boot_marker = root / "bootstrap-done"
        self.shutdown_log = root / "shutdown.log"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RIG_WATCHDOG_ENV_FILE": str(root / "rig.env"),
            "RIG_WATCHDOG_STATE_DIR": str(root),
            "RIG_WATCHDOG_BOOT_MARKER": str(self.boot_marker),
            "RIG_WATCHDOG_UPTIME_FILE": str(root / "uptime"),
            "STUB_SHUTDOWN_LOG": str(self.shutdown_log),
        }

    def tick(self, uptime_s: int, gpu_util: int = 0, running: int = 0, pending: int = 0, reachable: bool = True):
        (self.root / "uptime").write_text(f"{uptime_s}.42 1234.56\n")
        queue = {"queue_running": [[0]] * running, "queue_pending": [[0]] * pending}
        env = {**self.env, "STUB_GPU_UTIL": str(gpu_util), "STUB_QUEUE_JSON": json.dumps(queue) if reachable else ""}
        result = subprocess.run(["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, check=True)
        return result.stdout

    def stopped(self) -> bool:
        return self.shutdown_log.exists() and "-h now" in self.shutdown_log.read_text()

    def counter(self, name: str) -> int:
        return int((self.root / name).read_text())

    def set_counter(self, name: str, value: int) -> None:
        (self.root / name).write_text(f"{value}\n")


@pytest.fixture
def box(tmp_path) -> Box:
    return Box(tmp_path)


@pytest.fixture
def booted(box) -> Box:
    box.boot_marker.touch()
    return box


def test_unfinished_bootstrap_is_left_alone_for_three_hours(box):
    box.tick(uptime_s=3 * 3600 - 1)
    assert not box.stopped()


def test_stuck_bootstrap_stops_the_instance(box):
    out = box.tick(uptime_s=3 * 3600)
    assert box.stopped()
    assert "bootstrap never completed" in out


def test_fresh_boot_grace_resets_stale_counters(booted):
    booted.set_counter("rig_idle_ticks", 5)
    booted.set_counter("rig_busy_ticks", 70)
    booted.tick(uptime_s=599)
    assert booted.counter("rig_idle_ticks") == 0
    assert booted.counter("rig_busy_ticks") == 0
    assert not booted.stopped()


def test_idle_gpu_stops_after_thirty_minutes(booted):
    for _ in range(5):
        booted.tick(uptime_s=1000)
    assert booted.counter("rig_idle_ticks") == 5
    assert not booted.stopped()
    out = booted.tick(uptime_s=1000)
    assert booted.stopped()
    assert "GPU idle for 30 minutes" in out


def test_busy_gpu_resets_the_idle_count(booted):
    booted.set_counter("rig_idle_ticks", 5)
    booted.tick(uptime_s=1000, gpu_util=97)
    assert booted.counter("rig_idle_ticks") == 0
    assert not booted.stopped()


def test_queued_work_resets_the_idle_count(booted):
    booted.set_counter("rig_idle_ticks", 5)
    booted.tick(uptime_s=1000, gpu_util=0, pending=1)
    assert booted.counter("rig_idle_ticks") == 0
    assert booted.counter("rig_busy_ticks") == 1


def test_unreachable_comfyui_counts_as_an_empty_queue(booted):
    booted.tick(uptime_s=1000, reachable=False)
    assert booted.counter("rig_idle_ticks") == 1
    assert booted.counter("rig_busy_ticks") == 0


def test_long_running_job_below_the_ceiling_keeps_running(booted):
    booted.set_counter("rig_busy_ticks", 70)
    booted.tick(uptime_s=50000, gpu_util=99, running=1)
    assert booted.counter("rig_busy_ticks") == 71
    assert not booted.stopped()


def test_hung_job_stops_the_instance_at_six_hours(booted):
    booted.set_counter("rig_busy_ticks", 71)
    out = booted.tick(uptime_s=50000, gpu_util=99, running=1)
    assert booted.stopped()
    assert "queue busy for 360 minutes" in out
