#!/usr/bin/env python3
"""Run a generation job against the ComfyUI rig and collect the outputs.

Usage (tunnel must be open: `python3 launch_rig.py tunnel`):
  python3 runner/run_job.py jobs/examples/persona_ref.toml       # single workflow
  python3 runner/run_job.py jobs/examples/shaker_demo.toml       # model bake-off
  python3 runner/run_job.py job.toml --models wan22 --no-sync
  python3 runner/run_job.py job.toml --dry-run                   # print the plan only

The job file (TOML) declares either `workflow = "workflows/x.json"` or
`models = ["wan22", "hunyuan15"]`. For each run, values are injected into
workflow nodes tagged with RIG_* titles (see workflows/README.md), the prompt
is queued, outputs are downloaded to outputs/<run_id>/, videos are normalized
to a 9:16 MP4 with ffmpeg, and a side-by-side comparison clip is built when two
or more models produced video. The run folder then syncs to S3 with presigned
share links unless --no-sync is given.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfy_client import DEFAULT_POLL_S, ComfyClient, ComfyError
from media import ffmpeg_available, portrait_encode_command, run_ffmpeg, side_by_side_command
from s3_sync import PRESIGN_TTL_S, SHAREABLE_SUFFIXES, load_aws_config, sync_run

from rig_config import REPO_DIR, load_config

VIDEO_SUFFIXES = {".mp4", ".webm", ".webp", ".gif"}
REFERENCE_KEYS = ("start_image", "mask_image")

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
    return load_config(require_bucket=False)["rig"]["comfyui_port"]


def load_job(path: Path) -> dict:
    with open(path, "rb") as fh:
        job = tomllib.load(fh)
    if "prompt" not in job:
        raise SystemExit(f"{path}: job file must set 'prompt'")
    if "workflow" not in job and "models" not in job:
        raise SystemExit(f"{path}: job file must set 'workflow' or 'models'")
    return {**JOB_DEFAULTS, **job}


def plan_runs(job: dict, model_filter: list[str] | None, repo_dir: Path = REPO_DIR) -> list[dict]:
    """Return [{label, workflow_path, fps}] for this job, skipping missing files."""
    if "workflow" in job:
        path = repo_dir / job["workflow"]
        return [{"label": path.stem, "workflow_path": path, "fps": None}]
    runs = []
    for model in job["models"]:
        if model_filter and model not in model_filter:
            continue
        if model not in MODEL_WORKFLOWS:
            print(f"WARNING: unknown model '{model}' (known: {list(MODEL_WORKFLOWS)})")
            continue
        filename, fps = MODEL_WORKFLOWS[model]
        path = repo_dir / "workflows" / filename
        if not path.exists():
            print(
                f"WARNING: skipping '{model}': {path.name} not found. Export it from "
                "the ComfyUI template browser first (see workflows/README.md)."
            )
            continue
        runs.append({"label": model, "workflow_path": path, "fps": fps})
    return runs


def round_to_4k1(frames: int) -> int:
    """Nearest valid Wan frame count (must be 4k+1, e.g. 5s x 16fps = 80 gives 81)."""
    return max(5, round((frames - 1) / 4) * 4 + 1)


def latent_updates(inputs: dict, job: dict, fps: int | None) -> dict:
    width, height = job["resolution"]
    updates = {field: value for field, value in (("width", width), ("height", height)) if field in inputs}
    if fps:
        frames = int(job["duration_seconds"] * fps)
        for field in ("length", "frames", "num_frames"):
            if field in inputs:
                updates[field] = round_to_4k1(frames) if field == "length" else frames
                break
    return updates


def injected_inputs(title: str, inputs: dict, job: dict, fps: int | None, uploads: dict) -> dict:
    """Return the input fields a RIG_* tagged node receives from the job."""
    if title == "RIG_POSITIVE":
        return {"text": job["prompt"]}
    if title == "RIG_NEGATIVE":
        return {"text": job["negative_prompt"]}
    if title.startswith("RIG_SAMPLER"):
        return {field: job["seed"] for field in ("seed", "noise_seed") if field in inputs}
    if title == "RIG_START_IMAGE" and uploads.get("start_image"):
        return {"image": uploads["start_image"]}
    if title == "RIG_MASK" and uploads.get("mask_image"):
        return {"image": uploads["mask_image"]}
    if title == "RIG_LATENT":
        return latent_updates(inputs, job, fps)
    if title == "RIG_VIDEO" and fps and "fps" in inputs:
        return {"fps": fps}
    return {}


def node_title(node: dict) -> str:
    return node.get("_meta", {}).get("title", "")


def render_workflow(run: dict, job: dict, uploads: dict) -> dict:
    """Load the workflow template and return it with job values injected into
    RIG_* tagged nodes. Top-level keys starting with "_" are notes and dropped."""
    raw = json.loads(run["workflow_path"].read_text())
    workflow = {}
    for node_id, node in raw.items():
        if node_id.startswith("_"):
            continue
        updates = injected_inputs(node_title(node), node["inputs"], job, run["fps"], uploads)
        workflow[node_id] = {**node, "inputs": {**node["inputs"], **updates}}
    return workflow


def upload_references(client: ComfyClient, job: dict, repo_dir: Path = REPO_DIR) -> dict:
    uploads = {}
    for key in REFERENCE_KEYS:
        if key in job:
            path = repo_dir / job[key]
            if not path.exists():
                raise SystemExit(f"{key} not found: {job[key]}")
            uploads[key] = client.upload_image(path)
            print(f"uploaded {key}: {path.name} as {uploads[key]}")
    return uploads


def encode_portrait(source: Path, dest: Path, resolution: list[int]) -> Path | None:
    if not ffmpeg_available():
        print("WARNING: ffmpeg not found locally, skipping the 9:16 encode")
        return None
    return run_ffmpeg(portrait_encode_command(source, dest, resolution), dest)


def build_side_by_side(finals: list[Path], dest: Path) -> Path | None:
    cmd = side_by_side_command(finals, dest)
    if cmd is None or not ffmpeg_available():
        return None
    return run_ffmpeg(cmd, dest)


def execute_run(client: ComfyClient, run: dict, job: dict, uploads: dict, out_dir: Path, timeout_s: int) -> dict:
    print(f"\n=== {run['label']}: queueing")
    workflow = render_workflow(run, job, uploads)
    started = time.monotonic()
    prompt_id = client.queue_prompt(workflow)
    entry = client.wait(prompt_id, timeout_s=timeout_s)
    files = [client.download_output(info, out_dir / run["label"]) for info in client.output_files(entry)]
    elapsed = round(time.monotonic() - started, 1)
    print(f"=== {run['label']}: {len(files)} file(s) in {elapsed}s")

    final = None
    videos = [f for f in files if f.suffix.lower() in VIDEO_SUFFIXES]
    if videos:
        final = encode_portrait(videos[0], out_dir / f"final_{run['label']}.mp4", job["resolution"])
    return {
        "label": run["label"],
        "prompt_id": prompt_id,
        "elapsed_s": elapsed,
        "files": [str(f.relative_to(out_dir)) for f in files],
        "final": str(final.relative_to(out_dir)) if final else None,
    }


def make_run_id(job_file: Path, now: datetime) -> str:
    return f"{now:%Y%m%d-%H%M%S}-{job_file.stem}"


def write_manifest(out_dir: Path, run_id: str, job_file: Path, job: dict, results: list, errors: list, compare):
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "job_file": str(job_file),
        "job": job,
        "results": results,
        "errors": errors,
        "compare": compare.name if compare else None,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def sync_and_print_links(out_dir: Path) -> None:
    aws = load_aws_config()
    urls = sync_run(out_dir, aws)
    print("share links (up to 7 days; capped by your AWS session lifetime for SSO/assumed-role profiles):")
    for rel, url in urls.items():
        print(f"  {rel}\n    {url}")


def run_job(args: argparse.Namespace, job: dict, runs: list[dict]) -> int:
    client = ComfyClient(args.host, args.port or configured_comfy_port())
    try:
        client.ping()
    except ComfyError as err:
        raise SystemExit(str(err)) from err
    uploads = upload_references(client, job)

    run_id = make_run_id(args.job_file, datetime.now())
    out_dir = REPO_DIR / "outputs" / run_id
    out_dir.mkdir(parents=True)
    results, errors = [], []
    for run in runs:
        try:
            results.append(execute_run(client, run, job, uploads, out_dir, args.timeout))
        except ComfyError as err:
            errors.append({"label": run["label"], "error": str(err)})
            print(f"ERROR in {run['label']}: {err}")

    finals = [out_dir / r["final"] for r in results if r["final"]]
    compare = build_side_by_side(finals, out_dir / "compare_side_by_side.mp4")
    write_manifest(out_dir, run_id, args.job_file, job, results, errors, compare)
    print(f"\nrun folder: {out_dir}")

    if not args.no_sync and results:
        sync_and_print_links(out_dir)
    return 1 if errors and not results else 0


# --- dry run -------------------------------------------------------------

DISPLAY_LIMIT = 80


def display(value) -> str:
    text = json.dumps(value)
    if len(text) <= DISPLAY_LIMIT:
        return text
    return f"{text[:DISPLAY_LIMIT]} (+{len(text) - DISPLAY_LIMIT} chars)"


def plan_uploads(job: dict, repo_dir: Path = REPO_DIR) -> dict:
    """Print the reference uploads; return the names a real upload would normally get."""
    uploads = {}
    for key in REFERENCE_KEYS:
        if key not in job:
            continue
        note = "" if (repo_dir / job[key]).exists() else "  (file missing: a real run stops here)"
        print(f"POST /upload/image  {key} = {job[key]}{note}")
        uploads[key] = Path(job[key]).name
    return uploads


def print_run_plan(run: dict, job: dict, uploads: dict, out_rel: str, timeout_s: int) -> None:
    fps = f", {run['fps']} fps" if run["fps"] else ""
    print(f"\n=== {run['label']}: {run['workflow_path'].relative_to(REPO_DIR)}{fps}")
    raw = json.loads(run["workflow_path"].read_text())
    for node_id, node in raw.items():
        if node_id.startswith("_"):
            continue
        title = node_title(node)
        for field, value in injected_inputs(title, node["inputs"], job, run["fps"], uploads).items():
            print(f"  node {node_id} {title}: {field} = {display(value)}")
    print(f"  POST /prompt, then GET /history/<prompt_id> every {DEFAULT_POLL_S} s for up to {timeout_s} s")
    print(f"  GET /view for each output file, saved under {out_rel}/{run['label']}/")
    source = Path(out_rel, run["label"], "<first video output>")
    encode = portrait_encode_command(source, Path(out_rel, f"final_{run['label']}.mp4"), job["resolution"])
    print(f"  if a video is returned: {shlex.join(encode)}")


def print_plan(args: argparse.Namespace, job: dict, runs: list[dict], now: datetime) -> None:
    run_id = make_run_id(args.job_file, now)
    out_rel = f"outputs/{run_id}"
    print("dry run: nothing is sent to ComfyUI, no file is written and nothing is uploaded.")
    print(f"ComfyUI: http://{args.host}:{args.port or configured_comfy_port()} (GET /queue reachability check)")
    print(f"run folder: {out_rel} (not created)")
    uploads = plan_uploads(job)
    for run in runs:
        print_run_plan(run, job, uploads, out_rel, args.timeout)
    finals = [Path(out_rel, f"final_{run['label']}.mp4") for run in runs]
    compare = side_by_side_command(finals, Path(out_rel, "compare_side_by_side.mp4"))
    if compare:
        print(f"\ncomparison: {shlex.join(compare)}")
    if not ffmpeg_available():
        print("ffmpeg not found locally: a real run would skip the encode and the comparison")
    print(f"manifest: {out_rel}/manifest.json")
    if args.no_sync:
        print("S3 sync: skipped (--no-sync)")
        return
    bucket = load_config(require_bucket=False)["s3"]["outputs_bucket"]
    days = PRESIGN_TTL_S // 86400
    print(f"S3 sync: every file under {out_rel}/ to s3://{bucket}/runs/{run_id}/")
    print(f"  presigned GET links ({days} days) for {' '.join(sorted(SHAREABLE_SUFFIXES))}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_file", type=Path)
    parser.add_argument("--models", nargs="*", help="subset of the job's models to run")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, help="ComfyUI port (default: comfyui_port in rig.config.toml)")
    parser.add_argument("--timeout", type=int, default=3600, help="per-run seconds")
    parser.add_argument("--no-sync", action="store_true", help="skip S3 upload")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the job plan (injected node values, HTTP calls, ffmpeg commands, S3 keys) and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    job = load_job(args.job_file)
    runs = plan_runs(job, args.models)
    if not runs:
        raise SystemExit("nothing to run: no usable workflows for this job")
    if args.dry_run:
        print_plan(args, job, runs, datetime.now())
        return
    sys.exit(run_job(args, job, runs))


if __name__ == "__main__":
    main()
