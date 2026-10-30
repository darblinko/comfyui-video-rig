#!/usr/bin/env python3
"""Run a generation job against the ComfyUI rig and collect the outputs.

Usage (tunnel must be open: `python3 launch_rig.py tunnel`):
  python3 runner/run_job.py jobs/examples/persona_ref.toml       # single workflow
  python3 runner/run_job.py jobs/examples/shaker_demo.toml       # model bake-off
  python3 runner/run_job.py job.toml --models wan22 --no-sync

The job file (TOML) declares either `workflow = "workflows/x.json"` or
`models = ["wan22", "hunyuan15"]`. For each run, values are injected into
workflow nodes tagged with RIG_* titles (see workflows/README.md), the prompt
is queued, outputs are downloaded to outputs/<run_id>/, videos are normalized
to 9:16 MP4 with ffmpeg, and a side-by-side comparison clip is built when two
or more models produced video. The run folder then syncs to S3 with presigned
share links unless --no-sync is given.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy_client import ComfyClient, ComfyError  # noqa: E402
from s3_sync import load_aws_config, sync_run  # noqa: E402

PROTO_DIR = Path(__file__).resolve().parent.parent
VIDEO_SUFFIXES = {".mp4", ".webm", ".webp", ".gif"}

# label -> (workflow file, native fps). Frame counts are derived from
# job duration_seconds * fps; "length" fields are rounded to 4k+1 (Wan requirement).
MODEL_WORKFLOWS = {
    "wan22": ("wan22_i2v.json", 16),
    "hunyuan15": ("hunyuan15_i2v.json", 24),
}

JOB_DEFAULTS = {
    "negative_prompt": "",
    "seed": 0,
    "duration_seconds": 5,
    "resolution": [720, 1280],
}


def configured_comfy_port() -> int:
    with open(PROTO_DIR / "rig.config.toml", "rb") as fh:
        return tomllib.load(fh)["rig"]["comfyui_port"]


def load_job(path: Path) -> dict:
    with open(path, "rb") as fh:
        job = tomllib.load(fh)
    if "prompt" not in job:
        raise SystemExit(f"{path}: job file must set 'prompt'")
    if "workflow" not in job and "models" not in job:
        raise SystemExit(f"{path}: job file must set 'workflow' or 'models'")
    return {**JOB_DEFAULTS, **job}


def plan_runs(job: dict, model_filter: list[str] | None) -> list[dict]:
    """Return [{label, workflow_path, fps}] for this job, skipping missing files."""
    if "workflow" in job:
        path = PROTO_DIR / job["workflow"]
        return [{"label": path.stem, "workflow_path": path, "fps": None}]
    runs = []
    for model in job["models"]:
        if model_filter and model not in model_filter:
            continue
        if model not in MODEL_WORKFLOWS:
            print(f"WARNING: unknown model '{model}' (known: {list(MODEL_WORKFLOWS)})")
            continue
        filename, fps = MODEL_WORKFLOWS[model]
        path = PROTO_DIR / "workflows" / filename
        if not path.exists():
            print(
                f"WARNING: skipping '{model}' - {path.name} not found. Export it from "
                "the ComfyUI template browser first (see workflows/README.md)."
            )
            continue
        runs.append({"label": model, "workflow_path": path, "fps": fps})
    return runs


def round_to_4k1(frames: int) -> int:
    """Nearest valid Wan frame count (must be 4k+1, e.g. 5s x 16fps = 80 -> 81)."""
    return max(5, round((frames - 1) / 4) * 4 + 1)


def inject_node(node: dict, title: str, job: dict, fps: int | None, uploads: dict) -> None:
    inputs = node["inputs"]
    if title == "RIG_POSITIVE":
        inputs["text"] = job["prompt"]
    elif title == "RIG_NEGATIVE":
        inputs["text"] = job["negative_prompt"]
    elif title.startswith("RIG_SAMPLER"):
        for field in ("seed", "noise_seed"):
            if field in inputs:
                inputs[field] = job["seed"]
    elif title == "RIG_START_IMAGE" and uploads.get("start_image"):
        inputs["image"] = uploads["start_image"]
    elif title == "RIG_MASK" and uploads.get("mask_image"):
        inputs["image"] = uploads["mask_image"]
    elif title == "RIG_LATENT":
        width, height = job["resolution"]
        for field, value in (("width", width), ("height", height)):
            if field in inputs:
                inputs[field] = value
        if fps:
            frames = int(job["duration_seconds"] * fps)
            for field in ("length", "frames", "num_frames"):
                if field in inputs:
                    inputs[field] = round_to_4k1(frames) if field == "length" else frames
                    break
    elif title == "RIG_VIDEO" and fps and "fps" in inputs:
        inputs["fps"] = fps


def render_workflow(run: dict, job: dict, uploads: dict) -> dict:
    """Load the workflow template and inject job values into RIG_* tagged nodes."""
    raw = json.loads(run["workflow_path"].read_text())
    workflow = copy.deepcopy({k: v for k, v in raw.items() if not k.startswith("_")})
    for node in workflow.values():
        title = node.get("_meta", {}).get("title", "")
        if title.startswith("RIG_"):
            inject_node(node, title, job, run["fps"], uploads)
    return workflow


def upload_references(client: ComfyClient, job: dict) -> dict:
    uploads = {}
    for key in ("start_image", "mask_image"):
        if key in job:
            path = PROTO_DIR / job[key]
            if not path.exists():
                raise SystemExit(f"{key} not found: {path}")
            uploads[key] = client.upload_image(path)
            print(f"uploaded {key}: {path.name} -> {uploads[key]}")
    return uploads


def encode_ig_mp4(source: Path, dest: Path, resolution: list[int]) -> Path | None:
    """Normalize a generated clip to an IG-ready portrait MP4."""
    if not shutil.which("ffmpeg"):
        print("WARNING: ffmpeg not found locally - skipping IG encode")
        return None
    width, height = resolution
    scale = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(source), "-vf", scale,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
        "-movflags", "+faststart", str(dest),
    ]
    return run_ffmpeg(cmd, dest)


def run_ffmpeg(cmd: list[str], dest: Path) -> Path | None:
    """Run ffmpeg; on failure warn and return None so the raw clip still survives."""
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return dest
    except subprocess.CalledProcessError as err:
        tail = err.stderr.decode(errors="replace").strip().splitlines()[-3:]
        print(f"WARNING: ffmpeg failed for {dest.name}: {' | '.join(tail)}")
        dest.unlink(missing_ok=True)  # drop the partial file so it is never synced
        return None


def build_side_by_side(finals: list[Path], dest: Path) -> Path | None:
    if len(finals) < 2 or not shutil.which("ffmpeg"):
        return None
    inputs = []
    for path in finals[:2]:
        inputs += ["-i", str(path)]
    filter_expr = "[0:v]scale=-2:720[l];[1:v]scale=-2:720[r];[l][r]hstack"
    cmd = [
        "ffmpeg", "-y", *inputs, "-filter_complex", filter_expr,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest),
    ]
    return run_ffmpeg(cmd, dest)


def execute_run(
    client: ComfyClient, run: dict, job: dict, uploads: dict, out_dir: Path, timeout_s: int
) -> dict:
    print(f"\n=== {run['label']}: queueing")
    workflow = render_workflow(run, job, uploads)
    started = time.monotonic()
    prompt_id = client.queue_prompt(workflow)
    entry = client.wait(prompt_id, timeout_s=timeout_s)
    files = [
        client.download_output(info, out_dir / run["label"])
        for info in client.output_files(entry)
    ]
    elapsed = round(time.monotonic() - started, 1)
    print(f"=== {run['label']}: {len(files)} file(s) in {elapsed}s")

    final = None
    videos = [f for f in files if f.suffix.lower() in VIDEO_SUFFIXES]
    if videos:
        final = encode_ig_mp4(
            videos[0], out_dir / f"final_{run['label']}.mp4", job["resolution"]
        )
    return {
        "label": run["label"],
        "prompt_id": prompt_id,
        "elapsed_s": elapsed,
        "files": [str(f.relative_to(out_dir)) for f in files],
        "final": str(final.relative_to(out_dir)) if final else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_file", type=Path)
    parser.add_argument("--models", nargs="*", help="subset of the job's models to run")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=None,
        help="ComfyUI port (default: comfyui_port from rig.config.toml)",
    )
    parser.add_argument("--timeout", type=int, default=3600, help="per-run seconds")
    parser.add_argument("--no-sync", action="store_true", help="skip S3 upload")
    args = parser.parse_args()

    job = load_job(args.job_file)
    runs = plan_runs(job, args.models)
    if not runs:
        raise SystemExit("nothing to run - no usable workflows for this job")

    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{args.job_file.stem}"
    out_dir = PROTO_DIR / "outputs" / run_id
    out_dir.mkdir(parents=True)
    port = args.port if args.port else configured_comfy_port()
    client = ComfyClient(args.host, port)

    uploads = upload_references(client, job)
    results, errors = [], []
    for run in runs:
        try:
            results.append(execute_run(client, run, job, uploads, out_dir, args.timeout))
        except ComfyError as err:
            errors.append({"label": run["label"], "error": str(err)})
            print(f"ERROR in {run['label']}: {err}")

    finals = [out_dir / r["final"] for r in results if r["final"]]
    compare = build_side_by_side(finals, out_dir / "compare_side_by_side.mp4")

    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "job_file": str(args.job_file),
        "job": job,
        "results": results,
        "errors": errors,
        "compare": compare.name if compare else None,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nrun folder: {out_dir}")

    if not args.no_sync and results:
        aws = load_aws_config(PROTO_DIR / "rig.config.toml")
        urls = sync_run(out_dir, aws)
        print("share links (up to 7 days; capped by your AWS session lifetime for SSO/assumed-role profiles):")
        for rel, url in urls.items():
            print(f"  {rel}\n    {url}")
    if errors and not results:
        sys.exit(1)


if __name__ == "__main__":
    main()
