import subprocess
from pathlib import Path

import pytest

import run_chain
from conftest import REPO_DIR
from postprocess import build_model_clips, compare_command, concat_list, segments_by_model
from run_chain import last_frame_command, load_spec, run_folder_from_output, segment_start_image, segment_toml

SPEC_PATH = REPO_DIR / "jobs" / "headphones_test" / "chain.toml"


@pytest.fixture
def spec() -> dict:
    return load_spec(SPEC_PATH)


@pytest.mark.parametrize("model", ["wan22", "hunyuan15"])
@pytest.mark.parametrize("seg", [1, 2, 3, 4])
def test_spec_reproduces_the_committed_segment_jobs(spec, model, seg):
    committed = SPEC_PATH.parent / "generated" / f"{model}_seg{seg}.toml"
    assert segment_toml(spec, model, seg) == committed.read_text()


def test_segments_chain_from_the_previous_last_frame(spec):
    assert segment_start_image(spec, "wan22", 1) == "outputs/headphones_test/frame0.png"
    assert segment_start_image(spec, "wan22", 3) == "outputs/headphones_test/wan22_seg2_last.png"


def test_spec_requires_the_prompt_parts(tmp_path):
    path = tmp_path / "chain.toml"
    path.write_text('name = "x"\nmodels = ["wan22"]\n')
    with pytest.raises(SystemExit, match="persona"):
        load_spec(path)


def test_last_frame_command_seeks_from_the_end():
    cmd = last_frame_command(Path("seg.mp4"), Path("last.png"))
    assert cmd[cmd.index("-sseof") + 1] == "-0.5"
    assert cmd[-3:] == ["-update", "1", "last.png"]


def test_run_folder_is_the_last_one_printed():
    stdout = "=== wan22: queueing\nrun folder: /a/outputs/one\nother\nrun folder: /a/outputs/two\n"
    assert run_folder_from_output(stdout) == Path("/a/outputs/two")
    assert run_folder_from_output("no folder here") is None


def test_run_segment_writes_the_job_and_returns_the_newest_raw_clip(spec, tmp_path):
    spec = {**spec, "spec_dir": tmp_path}
    run_folder = tmp_path / "outputs" / "20260711-150000-wan22_seg1"
    (run_folder / "wan22").mkdir(parents=True)
    for name in ("wan22_00001_.mp4", "wan22_00002_.mp4"):
        (run_folder / "wan22" / name).write_bytes(b"v")
    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"run folder: {run_folder}\n", stderr="")

    raw = run_chain.run_segment(spec, "wan22", 1, run=fake_run)
    assert raw.name == "wan22_00002_.mp4"
    assert (tmp_path / "generated" / "wan22_seg1.toml").read_text() == segment_toml(spec, "wan22", 1)
    assert commands[0][1:4] == ["runner/run_job.py", str(tmp_path / "generated" / "wan22_seg1.toml"), "--no-sync"]


def test_failed_segment_stops_the_chain(spec, tmp_path):
    spec = {**spec, "spec_dir": tmp_path}

    def failing_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="cannot reach ComfyUI")

    with pytest.raises(RuntimeError, match="segment 2 failed"):
        run_chain.run_segment(spec, "wan22", 2, run=failing_run)


def test_resumed_segments_replace_earlier_records():
    state = [
        {"model": "wan22", "segment": 2, "raw": "outputs/a/old.mp4"},
        {"model": "wan22", "segment": 1, "raw": "outputs/a/s1.mp4"},
        {"model": "wan22", "segment": 2, "raw": "outputs/a/new.mp4"},
        {"model": "hunyuan15", "segment": 1, "raw": "outputs/b/s1.mp4"},
    ]
    segments = segments_by_model(state, repo_dir=Path("/repo"))
    assert segments["wan22"] == [Path("/repo/outputs/a/s1.mp4"), Path("/repo/outputs/a/new.mp4")]
    assert list(segments) == ["wan22", "hunyuan15"]


def test_concat_list_uses_absolute_paths(tmp_path):
    assert concat_list([tmp_path / "a.mp4"]) == f"file '{tmp_path / 'a.mp4'}'\n"


def test_model_clips_split_the_chain_and_offset_the_voiceover(tmp_path):
    commands = []
    parts = [tmp_path / f"s{i}.mp4" for i in range(1, 5)]
    joined = build_model_clips("wan22", parts, tmp_path, Path("vo.mp3"), 5, lambda cmd, **_k: commands.append(cmd))
    assert joined == tmp_path / "wan22_joined_20s_v.mp4"
    outputs = [cmd[-1] for cmd in commands]
    assert outputs == [
        str(tmp_path / "wan22_clip1_10s_v.mp4"),
        str(tmp_path / "wan22_clip1_10s.mp4"),
        str(tmp_path / "wan22_clip2_10s_v.mp4"),
        str(tmp_path / "wan22_clip2_10s.mp4"),
        str(tmp_path / "wan22_joined_20s_v.mp4"),
        str(tmp_path / "wan22_joined_20s.mp4"),
    ]
    clip2_mux = commands[3]
    assert clip2_mux[clip2_mux.index("-ss") + 1] == "10.0"


def test_silent_model_clips_skip_the_mux(tmp_path):
    commands = []
    parts = [tmp_path / f"s{i}.mp4" for i in range(1, 5)]
    build_model_clips("wan22", parts, tmp_path, None, 5, lambda cmd, **_k: commands.append(cmd))
    assert all("concat" in cmd for cmd in commands)
    assert len(commands) == 3


def test_comparison_with_voiceover_maps_the_audio_track():
    cmd = compare_command(Path("w.mp4"), Path("h.mp4"), Path("c.mp4"), Path("vo.mp3"))
    assert cmd[cmd.index("-filter_complex") + 1] == "[0:v]scale=-2:720[l];[1:v]scale=-2:720[r];[l][r]hstack[v]"
    assert "2:a" in cmd
    assert "-an" not in cmd


def test_silent_comparison_has_no_audio_track():
    cmd = compare_command(Path("w.mp4"), Path("h.mp4"), Path("c.mp4"), None)
    assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"] == ["w.mp4", "h.mp4"]
    assert "-an" in cmd
    assert "aac" not in cmd
