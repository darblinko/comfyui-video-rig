#!/usr/bin/env python3
"""Build headphones-test deliverables from the chain outputs.

Reads outputs/headphones_test/chain_state.json (written by run_chain.py)
and produces, per model: two 10s clips (segments 1+2 and 3+4), a 20s
joined clip, all with the shared voiceover muxed on, plus one
side-by-side comparison video of the two 20s clips.

Segment concat is stream-copy (no re-encode); only the side-by-side is
re-encoded. Run from the prototype dir with ffmpeg on PATH:
    python3 jobs/headphones_test/postprocess.py
"""

import json
import subprocess
from pathlib import Path

PROTO = Path(__file__).resolve().parents[2]
HP = PROTO / "outputs" / "headphones_test"
VOICEOVER = HP / "voiceover.mp3"
MODELS = ("wan22", "hunyuan15")


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def concat_copy(parts: list[Path], out: Path) -> None:
    list_file = out.with_suffix(".txt")
    list_file.write_text("".join(f"file '{p}'\n" for p in parts))
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out))


def mux_voice(video: Path, out: Path, audio_offset: float = 0.0) -> None:
    ffmpeg(
        "-i",
        str(video),
        "-ss",
        str(audio_offset),
        "-i",
        str(VOICEOVER),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(out),
    )


def main() -> None:
    state = json.loads((HP / "chain_state.json").read_text())
    segs = {(r["model"], r["segment"]): Path(r["raw"]) for r in state}

    joined = {}
    for model in MODELS:
        parts = [segs[(model, i)] for i in range(1, 5)]
        clip1_v = HP / f"{model}_clip1_v.mp4"
        clip2_v = HP / f"{model}_clip2_v.mp4"
        joined_v = HP / f"{model}_joined_v.mp4"
        concat_copy(parts[:2], clip1_v)
        concat_copy(parts[2:], clip2_v)
        concat_copy(parts, joined_v)

        mux_voice(clip1_v, HP / f"{model}_clip1_10s.mp4")
        mux_voice(clip2_v, HP / f"{model}_clip2_10s.mp4", audio_offset=10.0)
        mux_voice(joined_v, HP / f"{model}_joined_20s.mp4")
        joined[model] = joined_v
        print(f"{model}: clips built", flush=True)

    ffmpeg(
        "-i",
        str(joined["wan22"]),
        "-i",
        str(joined["hunyuan15"]),
        "-i",
        str(VOICEOVER),
        "-filter_complex",
        "[0:v]scale=-2:720[l];[1:v]scale=-2:720[r];[l][r]hstack[v]",
        "-map",
        "[v]",
        "-map",
        "2:a",
        "-c:v",
        "libx264",
        "-crf",
        "19",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(HP / "compare_side_by_side_20s.mp4"),
    )
    print("side-by-side built", flush=True)


if __name__ == "__main__":
    main()
