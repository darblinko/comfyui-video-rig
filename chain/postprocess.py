#!/usr/bin/env python3
"""Join the chained segments into deliverables and build the comparison clip.

Reads outputs/<name>/chain_state.json (written by run_chain.py) and writes, per
model, a clip of the first two segments, a clip of the rest, and the joined
clip; then one side-by-side video of the first two models' joined clips. When
the spec's voiceover file exists it is muxed onto every output; otherwise the
outputs are silent.

Segment joins are stream copies (no re-encode); only the side-by-side is
re-encoded. ffmpeg must be on PATH.

    python3 chain/postprocess.py jobs/headphones_test/chain.toml
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))
from media import COMPARE_HEIGHT
from run_chain import REPO_DIR, load_spec, stage_dir

AUDIO_OUTPUT = ["-c:a", "aac", "-b:a", "128k", "-shortest"]


def ffmpeg_command(*args: str) -> list[str]:
    return ["ffmpeg", "-y", "-loglevel", "error", *args]


def concat_list(parts: list[Path]) -> str:
    """Input list for ffmpeg's concat demuxer; absolute paths so the list file
    can live anywhere."""
    return "".join(f"file '{part.resolve()}'\n" for part in parts)


def concat_command(list_file: Path, out: Path) -> list[str]:
    return ffmpeg_command("-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out))


def mux_command(video: Path, voiceover: Path, out: Path, audio_offset: float = 0.0) -> list[str]:
    return ffmpeg_command(
        "-i", str(video), "-ss", str(audio_offset), "-i", str(voiceover),
        "-map", "0:v", "-map", "1:a", "-c:v", "copy", *AUDIO_OUTPUT, str(out),
    )  # fmt: skip


def duration_command(video: Path) -> list[str]:
    """Decode the first video stream to the null muxer; the final progress
    block's out_time_us is the decoded duration. A stream copy reports the
    last DTS instead, which is one frame short with B-frames."""
    progress = ["-v", "error", "-nostats", "-progress", "pipe:1"]
    return ["ffmpeg", *progress, "-i", str(video), "-map", "0:v:0", "-f", "null", "-"]


def parse_progress_duration(stdout: str) -> float:
    times = [line.removeprefix("out_time_us=") for line in stdout.splitlines() if line.startswith("out_time_us=")]
    if not times or not times[-1].strip().isdigit():
        raise RuntimeError("ffmpeg reported no duration")
    return int(times[-1]) / 1_000_000


def measure_duration(video: Path, run) -> float:
    proc = run(duration_command(video), check=True, capture_output=True, text=True)
    return parse_progress_duration(proc.stdout)


def compare_command(left: Path, right: Path, out: Path, voiceover: Path | None) -> list[str]:
    """Scale both clips to the same height, stack them left to right and
    re-encode; add the voiceover track when there is one."""
    h = COMPARE_HEIGHT
    stack = f"[0:v]scale=-2:{h}[l];[1:v]scale=-2:{h}[r];[l][r]hstack[v]"
    inputs = ["-i", str(left), "-i", str(right)]
    audio = ["-an"]
    if voiceover is not None:
        inputs += ["-i", str(voiceover)]
        audio = ["-map", "2:a", *AUDIO_OUTPUT]
    video = ["-c:v", "libx264", "-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    return ffmpeg_command(*inputs, "-filter_complex", stack, "-map", "[v]", *video, *audio, str(out))


def check_lineage(model: str, chain: list[tuple[int, dict]], segment_count: int) -> None:
    """Exit unless the chain holds segments 1..segment_count and each one was
    generated after, and from the last frame of, the segment before it."""
    present = {record["segment"] for _, record in chain}
    missing = [seg for seg in range(1, segment_count + 1) if seg not in present]
    if missing:
        raise SystemExit(f"{model}: missing segment {', '.join(map(str, missing))}; rerun run_chain.py")
    for (prev_index, prev), (index, record) in itertools.pairwise(chain):
        seg = record["segment"]
        if index < prev_index:
            raise SystemExit(f"{model} segment {seg} was generated before segment {seg - 1}; rerun from segment {seg}")
        start = record.get("start_image")
        if start is not None and start != prev.get("last_frame"):
            raise SystemExit(f"{model} segment {seg} started from {start}, not segment {seg - 1}'s last frame")


def segments_by_model(state: list[dict], segment_count: int, repo_dir: Path = REPO_DIR) -> dict[str, list[Path]]:
    """Latest raw clip for each (model, segment), in segment order, after
    checking that the segments form one complete chain."""
    latest = {(record["model"], record["segment"]): (index, record) for index, record in enumerate(state)}
    models = dict.fromkeys(model for model, _ in latest)
    result = {}
    for model in models:
        chain = [latest[key] for key in sorted(k for k in latest if k[0] == model)]
        check_lineage(model, chain, segment_count)
        result[model] = [repo_dir / record["raw"] for _, record in chain]
    return result


def join_and_voice(parts: list[Path], out_dir: Path, name: str, voiceover: Path | None, offset: float, run) -> Path:
    """Concat parts into <name>_v.mp4; with a voiceover also write <name>.mp4."""
    silent = out_dir / f"{name}_v.mp4"
    list_file = silent.with_suffix(".txt")
    list_file.write_text(concat_list(parts))
    run(concat_command(list_file, silent), check=True)
    if voiceover is not None:
        run(mux_command(silent, voiceover, out_dir / f"{name}.mp4", offset), check=True)
    return silent


def build_model_clips(
    model: str, parts: list[Path], out_dir: Path, voiceover, seconds: int, run, measure=measure_duration
) -> Path:
    """Names carry the nominal length (segments x seconds). The second clip's
    voiceover offset is the measured length of the first clip, because a
    segment of 81 frames at 16 fps runs 5.0625 s, not 5 s."""
    half = len(parts) // 2
    first_seconds = half * seconds
    first = join_and_voice(parts[:half], out_dir, f"{model}_clip1_{first_seconds}s", voiceover, 0.0, run)
    offset = measure(first, run) if voiceover is not None else 0.0
    rest_seconds = (len(parts) - half) * seconds
    join_and_voice(parts[half:], out_dir, f"{model}_clip2_{rest_seconds}s", voiceover, offset, run)
    return join_and_voice(parts, out_dir, f"{model}_joined_{len(parts) * seconds}s", voiceover, 0.0, run)


def main(argv: list[str] | None = None, run=subprocess.run) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", type=Path, help="chain spec TOML, e.g. jobs/headphones_test/chain.toml")
    args = parser.parse_args(argv)

    spec = load_spec(args.spec)
    out_dir = stage_dir(spec)
    state = json.loads((out_dir / "chain_state.json").read_text())
    voiceover = REPO_DIR / spec["voiceover"] if spec.get("voiceover") else None
    if voiceover is not None and not voiceover.exists():
        print(f"no voiceover at {spec['voiceover']}: outputs are silent")
        voiceover = None

    segments = segments_by_model(state, len(spec["beats"]))
    joined = {}
    for model, parts in segments.items():
        joined[model] = build_model_clips(model, parts, out_dir, voiceover, spec["segment_seconds"], run)
        print(f"{model}: clips built", flush=True)
    if len(joined) < 2:
        print("fewer than two models finished: no side-by-side")
        return
    (left_model, left), (_, right) = list(joined.items())[:2]
    seconds = len(segments[left_model]) * spec["segment_seconds"]
    out = out_dir / f"compare_side_by_side_{seconds}s.mp4"
    run(compare_command(left, right, out, voiceover), check=True)
    print(f"side-by-side built: {out.relative_to(REPO_DIR)}", flush=True)


if __name__ == "__main__":
    main()
