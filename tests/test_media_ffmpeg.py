"""Integration tests that run the real ffmpeg on small generated clips.

They check what the argv tests cannot: decoded frame counts, durations,
dimensions and where the voiceover lands. Skipped when ffmpeg is not on PATH."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from media import portrait_encode_command, side_by_side_command
from postprocess import build_model_clips, compare_command, duration_command, measure_duration
from run_chain import last_frame_command

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not on PATH")

SIZE = (128, 224)
TONE_START_S = 11.125


def ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True, text=True)


def make_segment(path: Path, frames: int, fps: int) -> Path:
    """An H.264 clip like the ones VideoHelperSuite writes: libx264, yuv420p, B-frames."""
    source = f"testsrc2=size={SIZE[0]}x{SIZE[1]}:rate={fps}"
    ffmpeg("-f", "lavfi", "-i", source, "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
    return path


def make_voiceover(path: Path) -> Path:
    """Silence until TONE_START_S, then a 440 Hz tone, 25 s in total."""
    tone = f"aevalsrc=if(gte(t\\,{TONE_START_S})\\,0.5*sin(2*PI*440*t)\\,0):s=48000:d=25"
    ffmpeg("-f", "lavfi", "-i", tone, "-c:a", "aac", str(path))
    return path


def video_size(path: Path) -> tuple[int, int]:
    proc = subprocess.run(["ffmpeg", "-i", str(path)], capture_output=True, text=True)
    match = re.search(r"Video: .*?, (\d+)x(\d+)", proc.stderr)
    assert match, proc.stderr
    return int(match.group(1)), int(match.group(2))


def decoded_frames(path: Path) -> int:
    proc = subprocess.run(duration_command(path), check=True, capture_output=True, text=True)
    return int([line for line in proc.stdout.splitlines() if line.startswith("frame=")][-1].removeprefix("frame="))


def tone_onset(path: Path) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-i", str(path), "-map", "0:a:0", "-af", "silencedetect=n=-30dB:d=0.2", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    ends = re.findall(r"silence_end: ([\d.]+)", proc.stderr)
    assert ends, proc.stderr
    return float(ends[0])


def raw_rgb(png: Path) -> bytes:
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(png), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True,
        capture_output=True,
    )
    return proc.stdout


@pytest.mark.parametrize(("frames", "fps", "seconds"), [(81, 16, 5.0625), (121, 24, 121 / 24)])
def test_measured_duration_counts_every_frame(tmp_path, frames, fps, seconds):
    segment = make_segment(tmp_path / "seg.mp4", frames, fps)
    assert measure_duration(segment, subprocess.run) == pytest.approx(seconds, abs=1e-3)


def test_last_frame_command_extracts_the_final_frame(tmp_path):
    segment = make_segment(tmp_path / "seg.mp4", 81, 16)
    subprocess.run(last_frame_command(segment, tmp_path / "last.png"), check=True)
    ffmpeg("-i", str(segment), "-vf", "select=eq(n\\,80)", "-frames:v", "1", str(tmp_path / "frame80.png"))
    assert video_size(tmp_path / "last.png") == SIZE
    assert raw_rgb(tmp_path / "last.png") == raw_rgb(tmp_path / "frame80.png")


def test_model_clips_keep_every_frame_and_align_the_voiceover(tmp_path):
    parts = [make_segment(tmp_path / f"s{i}.mp4", 81, 16) for i in range(1, 5)]
    voiceover = make_voiceover(tmp_path / "vo.m4a")
    out = tmp_path / "out"
    out.mkdir()
    joined = build_model_clips("wan22", parts, out, voiceover, 5, subprocess.run)
    assert decoded_frames(joined) == 4 * 81
    assert measure_duration(out / "wan22_clip1_10s_v.mp4", subprocess.run) == pytest.approx(10.125, abs=1e-3)
    # The tone starts 11.125 s into the voiceover, so 1.0 s into the second clip.
    assert tone_onset(out / "wan22_clip2_10s.mp4") == pytest.approx(TONE_START_S - 10.125, abs=0.03)
    assert tone_onset(out / "wan22_joined_20s.mp4") == pytest.approx(TONE_START_S, abs=0.03)


def test_comparison_stacks_two_clips_of_different_rates_at_720(tmp_path):
    left = make_segment(tmp_path / "w.mp4", 81, 16)
    right = make_segment(tmp_path / "h.mp4", 121, 24)
    subprocess.run(compare_command(left, right, tmp_path / "c.mp4", None), check=True)
    width, height = video_size(tmp_path / "c.mp4")
    single = round(SIZE[0] * 720 / SIZE[1] / 2) * 2
    assert (width, height) == (2 * single, 720)


def test_portrait_encode_pads_to_the_requested_size(tmp_path):
    source = make_segment(tmp_path / "wide.mp4", 17, 16)
    dest = tmp_path / "portrait.mp4"
    subprocess.run(portrait_encode_command(source, dest, [720, 1280]), check=True, capture_output=True)
    assert video_size(dest) == (720, 1280)
    assert decoded_frames(dest) == 17


def test_runner_side_by_side_is_720_high(tmp_path):
    clips = [make_segment(tmp_path / f"{name}.mp4", 17, 16) for name in ("a", "b")]
    subprocess.run(side_by_side_command(clips, tmp_path / "c.mp4"), check=True, capture_output=True)
    assert video_size(tmp_path / "c.mp4")[1] == 720
