#!/usr/bin/env python3
"""Summarize the recorded runs in results/manifests/ and build docs/data.json.

    python3 results/summarize.py            # print the run table (Markdown)
    python3 results/summarize.py --write    # also rewrite docs/data.json
    python3 results/summarize.py --check    # exit 1 if docs/data.json is out of date

Every number in docs/data.json is computed here from the manifests, so the
README charts and the landing page trace back to results/manifests/.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO_DIR / "results" / "manifests"
DATA_FILE = REPO_DIR / "docs" / "data.json"
SOURCE = "results/manifests/*.json"
VIDEO_MODELS = {"wan22": "Wan 2.2 A14B", "hunyuan15": "HunyuanVideo 1.5"}


def load_manifests(directory: Path = MANIFEST_DIR) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def run_rows(manifests: list[dict]) -> list[dict]:
    """One row per model run; a run that failed before queueing has one error row."""
    rows = []
    for manifest in manifests:
        base = {"run_id": manifest["run_id"], "job_file": manifest["job_file"]}
        for result in manifest["results"]:
            rows.append({**base, "label": result["label"], "elapsed_s": result["elapsed_s"], "status": "ok"})
        for error in manifest["errors"]:
            rows.append({**base, "label": error["label"], "elapsed_s": None, "status": "error"})
    return rows


def video_rows(manifests: list[dict]) -> list[dict]:
    by_id = {m["run_id"]: m for m in manifests}
    rows = [row for row in run_rows(manifests) if row["label"] in VIDEO_MODELS and row["status"] == "ok"]
    for row in rows:
        job = by_id[row["run_id"]]["job"]
        row["clip"] = f"{job['duration_seconds']} s, {job['resolution'][0]}x{job['resolution'][1]}"
    return rows


def markdown_table(manifests: list[dict]) -> str:
    lines = ["| Run | Job file | Model run | Seconds | Status |", "|---|---|---|---:|---|"]
    for row in run_rows(manifests):
        seconds = "" if row["elapsed_s"] is None else f"{row['elapsed_s']:.1f}"
        lines.append(f"| {row['run_id']} | `{row['job_file']}` | {row['label']} | {seconds} | {row['status']} |")
    return "\n".join(lines)


def chain_segment(run_id: str) -> int | None:
    stem = run_id.rsplit("_seg", 1)
    return int(stem[1]) if len(stem) == 2 and stem[1].isdigit() else None


def summary_chart(manifests: list[dict]) -> dict:
    rows = run_rows(manifests)
    videos = video_rows(manifests)
    clips = sorted({row["clip"] for row in videos})
    failed = [m for m in manifests if not m["results"]]
    unreachable = all("cannot reach ComfyUI" in e["error"] for m in failed for e in m["errors"])
    failure_note = f"{len(failed)} failed" + (": ComfyUI not reachable" if failed and unreachable else "")
    tiles = [
        {"label": "Runs recorded", "value": str(len(manifests)), "note": failure_note},
        {"label": "Video clips generated", "value": str(len(videos)), "note": ", ".join(clips)},
    ]
    for label, name in VIDEO_MODELS.items():
        times = [r["elapsed_s"] for r in videos if r["label"] == label]
        median = f"{statistics.median(times):,.0f} s"
        tiles.append({"label": f"{name}, median per clip", "value": median, "note": f"{len(times)} clips"})
    return {
        "name": "summary",
        "type": "stats",
        "title": "Recorded runs on 2026-07-11",
        "subtitle": f"{len(rows)} model runs in {len(manifests)} manifests",
        "source": SOURCE,
        "alt": "; ".join(f"{t['label']}: {t['value']}" for t in tiles),
        "tiles": tiles,
    }


def video_time_chart(manifests: list[dict]) -> dict:
    videos = video_rows(manifests)
    bakeoff = {r["label"]: r["elapsed_s"] for r in videos if "bakeoff" in r["run_id"]}
    segments = {(r["label"], chain_segment(r["run_id"])): r["elapsed_s"] for r in videos if chain_segment(r["run_id"])}
    seg_numbers = sorted({seg for _, seg in segments})
    categories = ["shaker bake-off", *[f"chain segment {n}" for n in seg_numbers]]
    series = [
        {"name": name, "values": [bakeoff[label], *[segments[(label, n)] for n in seg_numbers]]}
        for label, name in VIDEO_MODELS.items()
    ]
    values = [v for s in series for v in s["values"]]
    return {
        "name": "video-clip-time",
        "type": "columns",
        "title": "Seconds to generate one 5 s, 720x1280 clip",
        "subtitle": "Same prompt and seed for both models per category; timed from queueing to the last download",
        "source": SOURCE,
        "alt": f"Each clip took between {min(values):,.1f} and {max(values):,.1f} seconds. "
        + "; ".join(f"{s['name']}: {', '.join(f'{v:.1f}' for v in s['values'])}" for s in series),
        "format": "number",
        "categories": categories,
        "series": series,
    }


def still_time_chart(manifests: list[dict]) -> dict:
    stills = [r for r in run_rows(manifests) if r["label"].startswith("flux") and r["status"] == "ok"]
    data = [{"label": r["run_id"].split("-", 2)[2] + " " + r["run_id"][9:13], "value": r["elapsed_s"]} for r in stills]
    return {
        "name": "still-time",
        "type": "hbar",
        "title": "Seconds to generate one FLUX.1-dev still",
        "subtitle": "Runs in time order; label is the job name and the start time (HHMM)",
        "source": SOURCE,
        "alt": "; ".join(f"{d['label']}: {d['value']:.1f} s" for d in data),
        "format": "number",
        "data": data,
    }


def chart_data(manifests: list[dict]) -> dict:
    return {
        "meta": {"repo": "comfyui-video-rig", "measured": "2026-07-11", "source": SOURCE},
        "charts": [summary_chart(manifests), video_time_chart(manifests), still_time_chart(manifests)],
    }


def render(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite docs/data.json")
    mode.add_argument("--check", action="store_true", help="exit 1 if docs/data.json is out of date")
    args = parser.parse_args(argv)

    manifests = load_manifests()
    expected = render(chart_data(manifests))
    if args.check:
        current = DATA_FILE.read_text() if DATA_FILE.exists() else ""
        if current != expected:
            print("docs/data.json is out of date: run python3 results/summarize.py --write")
            return 1
        print("docs/data.json matches results/manifests/")
        return 0
    print(markdown_table(manifests))
    if args.write:
        DATA_FILE.parent.mkdir(exist_ok=True)
        DATA_FILE.write_text(expected)
        print(f"\nwrote {DATA_FILE.relative_to(REPO_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
