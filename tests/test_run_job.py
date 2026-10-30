import json
from pathlib import Path

import pytest

import run_job
from comfy_client import ComfyError
from conftest import REPO_DIR
from run_job import injected_inputs, load_job, plan_runs, render_workflow, round_to_4k1

BAKEOFF_JOB = {
    "prompt": "a presenter lifts a bottle",
    "negative_prompt": "blurry",
    "seed": 42,
    "duration_seconds": 5,
    "resolution": [720, 1280],
    "models": ["wan22", "hunyuan15"],
}


def write_job(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "job.toml"
    path.write_text(body)
    return path


@pytest.mark.parametrize(("frames", "expected"), [(80, 81), (120, 121), (81, 81), (0, 5), (98, 97)])
def test_wan_frame_counts_round_to_4k_plus_1(frames, expected):
    assert round_to_4k1(frames) == expected


def test_job_defaults_fill_missing_fields(tmp_path):
    job = load_job(write_job(tmp_path, 'prompt = "p"\nmodels = ["wan22"]\n'))
    assert job["seed"] == 0
    assert job["resolution"] == [720, 1280]


def test_job_without_prompt_is_rejected(tmp_path):
    with pytest.raises(SystemExit, match="prompt"):
        load_job(write_job(tmp_path, 'models = ["wan22"]\n'))


def test_plan_skips_unknown_models_and_honours_the_filter(capsys):
    job = {**BAKEOFF_JOB, "models": ["wan22", "hunyuan15", "sora"]}
    assert [run["label"] for run in plan_runs(job, None)] == ["wan22", "hunyuan15"]
    assert [run["label"] for run in plan_runs(job, ["hunyuan15"])] == ["hunyuan15"]
    assert "unknown model 'sora'" in capsys.readouterr().out


def test_plan_skips_models_whose_workflow_is_missing(tmp_path, capsys):
    assert plan_runs(BAKEOFF_JOB, None, repo_dir=tmp_path) == []
    assert "wan22_i2v.json not found" in capsys.readouterr().out


def test_tagged_nodes_receive_job_values():
    uploads = {"start_image": "frame0.png"}
    assert injected_inputs("RIG_POSITIVE", {"text": ""}, BAKEOFF_JOB, 16, uploads) == {"text": BAKEOFF_JOB["prompt"]}
    assert injected_inputs("RIG_SAMPLER_2", {"noise_seed": 0, "cfg": 3}, BAKEOFF_JOB, 16, uploads) == {"noise_seed": 42}
    assert injected_inputs("RIG_START_IMAGE", {"image": "x"}, BAKEOFF_JOB, 16, uploads) == {"image": "frame0.png"}
    assert injected_inputs("RIG_MASK", {"image": "x"}, BAKEOFF_JOB, 16, uploads) == {}
    assert injected_inputs("RIG_VIDEO", {"fps": 8}, BAKEOFF_JOB, 24, uploads) == {"fps": 24}
    assert injected_inputs("Load VAE", {"vae_name": "v"}, BAKEOFF_JOB, 16, uploads) == {}


def test_latent_frames_use_length_first():
    inputs = {"width": 0, "height": 0, "length": 0, "frames": 0}
    assert injected_inputs("RIG_LATENT", inputs, BAKEOFF_JOB, 16, {}) == {"width": 720, "height": 1280, "length": 81}
    assert injected_inputs("RIG_LATENT", {"num_frames": 0}, BAKEOFF_JOB, 24, {}) == {"num_frames": 120}


@pytest.mark.parametrize(("label", "length", "fps"), [("wan22", 81, 16), ("hunyuan15", 121, 24)])
def test_committed_workflows_render_with_job_values(label, length, fps):
    run = plan_runs(BAKEOFF_JOB, [label])[0]
    before = run["workflow_path"].read_text()
    workflow = render_workflow(run, BAKEOFF_JOB, {"start_image": "frame0.png"})
    by_title = {node["_meta"]["title"]: node["inputs"] for node in workflow.values()}
    assert not [key for key in workflow if key.startswith("_")]
    assert by_title["RIG_LATENT"]["length"] == length
    assert by_title["RIG_VIDEO"]["fps"] == fps
    assert by_title["RIG_START_IMAGE"]["image"] == "frame0.png"
    assert run["workflow_path"].read_text() == before


class FakeComfy:
    """Stands in for ComfyClient; every call is recorded, nothing touches the network."""

    reachable = True

    def __init__(self, host, port):
        self.address = (host, port)
        self.queued = []

    failing_labels: tuple[str, ...] = ()

    def ping(self):
        if not self.reachable:
            raise ComfyError("cannot reach ComfyUI at http://127.0.0.1:8188")

    def system_stats(self):
        return {"system": {"comfyui_version": "0.0-test", "pytorch_version": "2.x-test"}, "devices": []}

    def upload_image(self, path):
        return path.name

    def queue_prompt(self, workflow):
        self.queued.append(workflow)
        title_of = {node["_meta"]["title"]: node for node in workflow.values() if "_meta" in node}
        model = title_of.get("RIG_LATENT", {}).get("class_type", "")
        if any(label in model.lower() for label in self.failing_labels):
            raise ComfyError("CUDA out of memory")
        return f"prompt-{len(self.queued)}"

    def wait(self, prompt_id, timeout_s):
        return {"outputs": {"9": {"gifs": [{"filename": f"{prompt_id}.mp4"}]}}}

    output_files = staticmethod(run_job.ComfyClient.output_files)

    def download_output(self, info, dest_dir):
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / info["filename"]
        dest.write_bytes(b"video")
        return dest


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A repo copy with the real workflows, a fake ComfyUI and a recording ffmpeg."""
    (tmp_path / "workflows").symlink_to(REPO_DIR / "workflows")
    monkeypatch.setattr(run_job, "REPO_DIR", tmp_path)
    monkeypatch.setattr(run_job, "ComfyClient", FakeComfy)
    monkeypatch.setattr(run_job, "ffmpeg_available", lambda: True)
    commands = []

    def fake_ffmpeg(cmd, dest):
        commands.append(cmd)
        dest.write_bytes(b"encoded")
        return dest

    monkeypatch.setattr(run_job, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(run_job, "plan_runs", lambda job, models: plan_runs(job, models, repo_dir=tmp_path))
    return tmp_path, commands


def test_bakeoff_writes_finals_comparison_and_manifest(sandbox):
    root, commands = sandbox
    args = run_job.parse_args([str(write_job(root, _toml(BAKEOFF_JOB))), "--no-sync", "--port", "8188"])
    job = load_job(args.job_file)
    assert run_job.run_job(args, job, run_job.plan_runs(job, None)) == 0

    (run_dir,) = (root / "outputs").iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert [r["final"] for r in manifest["results"]] == ["final_wan22.mp4", "final_hunyuan15.mp4"]
    assert manifest["compare"] == "compare_side_by_side.mp4"
    assert manifest["errors"] == []
    assert commands[-1][commands[-1].index("-filter_complex") + 1].endswith("hstack")


def run_bakeoff(root: Path, *extra: str) -> tuple[int, dict]:
    args = run_job.parse_args([str(write_job(root, _toml(BAKEOFF_JOB))), "--no-sync", "--port", "8188", *extra])
    job = load_job(args.job_file)
    status = run_job.run_job(args, job, run_job.plan_runs(job, None))
    (run_dir,) = (root / "outputs").iterdir()
    return status, json.loads((run_dir / "manifest.json").read_text())


def test_partial_bakeoff_failure_exits_nonzero(sandbox, monkeypatch):
    root, _ = sandbox
    monkeypatch.setattr(FakeComfy, "failing_labels", ("hunyuan",))
    status, manifest = run_bakeoff(root)
    assert status == 1
    assert [r["label"] for r in manifest["results"]] == ["wan22"]
    assert manifest["errors"] == [{"label": "hunyuan15", "error": "CUDA out of memory"}]


def test_partial_bakeoff_failure_can_be_allowed(sandbox, monkeypatch):
    root, _ = sandbox
    monkeypatch.setattr(FakeComfy, "failing_labels", ("hunyuan",))
    status, _ = run_bakeoff(root, "--allow-partial")
    assert status == 0


def test_manifest_records_the_submitted_workflow_and_runtime(sandbox):
    root, _ = sandbox
    _, manifest = run_bakeoff(root)
    (run_dir,) = (root / "outputs").iterdir()
    wan = manifest["results"][0]
    assert wan["workflow_template"] == "workflows/wan22_i2v.json"
    assert (wan["fps"], wan["frames"]) == (16, 81)
    submitted = json.loads((run_dir / wan["workflow_submitted"]).read_text())
    sampler = next(node for node in submitted.values() if node["_meta"]["title"].startswith("RIG_SAMPLER"))
    assert sampler["inputs"]["noise_seed"] == 42
    assert manifest["comfyui"]["system"]["comfyui_version"] == "0.0-test"


def test_unreachable_comfyui_leaves_no_run_folder(sandbox, monkeypatch):
    root, _ = sandbox
    monkeypatch.setattr(FakeComfy, "reachable", False)
    args = run_job.parse_args([str(write_job(root, _toml(BAKEOFF_JOB))), "--port", "8188"])
    job = load_job(args.job_file)
    with pytest.raises(SystemExit, match="cannot reach ComfyUI"):
        run_job.run_job(args, job, run_job.plan_runs(job, None))
    assert not (root / "outputs").exists()


def test_dry_run_prints_the_plan_and_writes_nothing(sandbox, capsys, monkeypatch):
    root, commands = sandbox
    monkeypatch.delenv("RIG_OUTPUTS_BUCKET", raising=False)
    run_job.main([str(write_job(root, _toml(BAKEOFF_JOB))), "--dry-run"])
    out = capsys.readouterr().out
    assert "length = 81" in out
    assert "length = 121" in out
    assert "noise_seed = 42" in out
    assert "hstack" in out
    assert "s3://<RIG_OUTPUTS_BUCKET>/runs/" in out
    assert not (root / "outputs").exists()
    assert commands == []


def _toml(job: dict) -> str:
    return "".join(f"{key} = {json.dumps(value)}\n" for key, value in job.items())
