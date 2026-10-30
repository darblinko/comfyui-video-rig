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


def segments_by_model(state: list[dict], repo_dir: Path = REPO_DIR) -> dict[str, list[Path]]:
    """Latest raw clip for each (model, segment), in segment order."""
    latest = {(record["model"], record["segment"]): repo_dir / record["raw"] for record in state}
    models = dict.fromkeys(model for model, _ in latest)
    return {model: [latest[key] for key in sorted(k for k in latest if k[0] == model)] for model in models}


def join_and_voice(parts: list[Path], out_dir: Path, name: str, voiceover: Path | None, offset: float, run) -> Path:
    """Concat parts into <name>_v.mp4; with a voiceover also write <name>.mp4."""
    silent = out_dir / f"{name}_v.mp4"
    list_file = silent.with_suffix(".txt")
    list_file.write_text(concat_list(parts))
    run(concat_command(list_file, silent), check=True)
    if voiceover is not None:
        run(mux_command(silent, voiceover, out_dir / f"{name}.mp4", offset), check=True)
    return silent


def build_model_clips(model: str, parts: list[Path], out_dir: Path, voiceover, seconds: int, run) -> Path:
    half = len(parts) // 2
    first_seconds = half * seconds
    join_and_voice(parts[:half], out_dir, f"{model}_clip1_{first_seconds}s", voiceover, 0.0, run)
    rest_seconds = (len(parts) - half) * seconds
    join_and_voice(parts[half:], out_dir, f"{model}_clip2_{rest_seconds}s", voiceover, float(first_seconds), run)
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

    segments = segments_by_model(state)
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
