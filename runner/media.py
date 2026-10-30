"""ffmpeg command construction for the 9:16 encode and the side-by-side clip.

The builders return argv lists without running anything, so run_job.py can
print them in a dry run and the tests can check them without ffmpeg.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

H264_OUTPUT = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
COMPARE_HEIGHT = 720


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def portrait_encode_command(source: Path, dest: Path, resolution: list[int]) -> list[str]:
    """Fit the clip inside width x height without cropping, pad the rest, and
    write an H.264 MP4 with the index at the front for progressive playback."""
    width, height = resolution
    scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vf",
        scale,
        *H264_OUTPUT,
        "-crf",
        "18",
        "-movflags",
        "+faststart",
        str(dest),
    ]


def side_by_side_command(finals: list[Path], dest: Path) -> list[str] | None:
    """Scale the first two clips to the same height and stack them left to right."""
    if len(finals) < 2:
        return None
    left, right = finals[:2]
    h = COMPARE_HEIGHT
    filter_expr = f"[0:v]scale=-2:{h}[l];[1:v]scale=-2:{h}[r];[l][r]hstack"
    return ["ffmpeg", "-y", "-i", str(left), "-i", str(right), "-filter_complex", filter_expr, *H264_OUTPUT, str(dest)]


def run_ffmpeg(cmd: list[str], dest: Path, run=subprocess.run) -> Path | None:
    """Run ffmpeg; on failure warn and return None so the raw clip still survives."""
    try:
        run(cmd, check=True, capture_output=True)
        return dest
    except subprocess.CalledProcessError as err:
        tail = (err.stderr or b"").decode(errors="replace").strip().splitlines()[-3:]
        print(f"WARNING: ffmpeg failed for {dest.name}: {' | '.join(tail)}")
        dest.unlink(missing_ok=True)  # drop the partial file so it is never synced
        return None
