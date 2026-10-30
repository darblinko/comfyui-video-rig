import json
import subprocess
import tomllib
from pathlib import Path

import pytest

import run_chain
from conftest import REPO_DIR
from postprocess import (
    build_model_clips,
    compare_command,
    concat_list,
    mux_command,
    duration_command,
    parse_progress_duration,
    segments_by_model,
)
from run_chain import (
    last_frame_command,
    load_spec,
    record_segment,
    segment_start_image,
    segment_toml,
)

SPEC_PATH = REPO_DIR / "jobs" / "headphones_test" / "chain.toml"


@pytest.fixture
def spec() -> dict:
    return load_spec(SPEC_PATH)


@pytest.mark.parametrize("model", ["wan22", "hunyuan15"])
@pytest.mark.parametrize("seg", [1, 2, 3, 4])
def test_spec_reproduces_the_committed_segment_jobs(spec, model, seg):
    committed = SPEC_PATH.parent / "generated" / f"{model}_seg{seg}.toml"
    assert tomllib.loads(segment_toml(spec, model, seg)) == tomllib.loads(committed.read_text())


@pytest.mark.parametrize(
    "text",
    ['text, "logo"', "back\\slash", "line one\nline two", 'ends with a quote"', "tab\tand\x7fdel", 'three """ quotes'],
)
def test_segment_toml_round_trips_any_prompt_text(spec, text):
    spec = {**spec, "negative_prompt": text, "persona": text, "persona_id": text}
    job = tomllib.loads(segment_toml(spec, "wan22", 2))
    assert job["negative_prompt"] == text
    assert job["persona_id"] == text
    assert job["prompt"] == f"{text}, {spec['beats'][1]}, {spec['suffix']}"


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


def write_result(path: Path, run_dir: Path, results: list[dict]) -> None:
    path.write_text(json.dumps({"run_dir": str(run_dir), "results": results, "errors": []}))


def test_run_segment_writes_the_job_and_returns_the_video_named_in_the_result(spec, tmp_path):
    spec = {**spec, "spec_dir": tmp_path}
    run_folder = tmp_path / "outputs" / "20260711-150000-wan22_seg1"
    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        result_file = Path(cmd[cmd.index("--result-file") + 1])
        write_result(result_file, run_folder, [{"label": "wan22", "video": "wan22/wan22_00001_.mp4"}])
        return subprocess.CompletedProcess(cmd, 0, stdout="log line\n", stderr="")

    raw = run_chain.run_segment(spec, "wan22", 1, run=fake_run, repo_dir=tmp_path)
    assert raw == run_folder / "wan22" / "wan22_00001_.mp4"
    assert (tmp_path / "generated" / "wan22_seg1.toml").read_text() == segment_toml(spec, "wan22", 1)
    assert commands[0][1:4] == ["runner/run_job.py", str(tmp_path / "generated" / "wan22_seg1.toml"), "--no-sync"]


def test_run_segment_ignores_a_stale_result_file(spec, tmp_path):
    spec = {**spec, "spec_dir": tmp_path}
    stale = tmp_path / "outputs" / spec["name"] / "wan22_seg1_result.json"
    stale.parent.mkdir(parents=True)
    write_result(stale, tmp_path, [{"label": "wan22", "video": "old.mp4"}])
    ok = lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")  # noqa: E731
    with pytest.raises(RuntimeError, match="wrote no result file"):
        run_chain.run_segment(spec, "wan22", 1, run=ok, repo_dir=tmp_path)


def test_run_segment_needs_a_video_for_the_model(spec, tmp_path):
    spec = {**spec, "spec_dir": tmp_path}

    def fake_run(cmd, **_kwargs):
        write_result(Path(cmd[cmd.index("--result-file") + 1]), tmp_path, [{"label": "wan22", "video": None}])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="no video output"):
        run_chain.run_segment(spec, "wan22", 1, run=fake_run, repo_dir=tmp_path)


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
        {"model": "hunyuan15", "segment": 2, "raw": "outputs/b/s2.mp4"},
    ]
    segments = segments_by_model(state, 2, repo_dir=Path("/repo"))
    assert segments["wan22"] == [Path("/repo/outputs/a/s1.mp4"), Path("/repo/outputs/a/new.mp4")]
    assert list(segments) == ["wan22", "hunyuan15"]


def seg_record(seg: int, raw: str) -> dict:
    return {"model": "wan22", "segment": seg, "raw": raw, "last_frame": f"f{seg}.png", "start_image": f"f{seg - 1}.png"}


def test_regenerating_a_segment_drops_the_records_downstream_of_it():
    state = [seg_record(1, "s1.mp4"), seg_record(2, "s2.mp4"), seg_record(3, "s3.mp4"), seg_record(4, "s4.mp4")]
    state = record_segment(state, seg_record(2, "s2-new.mp4"))
    assert [(r["segment"], r["raw"]) for r in state] == [(1, "s1.mp4"), (2, "s2-new.mp4")]


def test_other_models_keep_their_records_when_one_is_regenerated():
    other = {**seg_record(3, "h3.mp4"), "model": "hunyuan15"}
    assert record_segment([other], seg_record(2, "s2.mp4")) == [other, seg_record(2, "s2.mp4")]


def test_postprocess_rejects_a_chain_with_a_missing_segment():
    state = [seg_record(1, "s1.mp4"), seg_record(2, "s2.mp4"), seg_record(4, "s4.mp4")]
    with pytest.raises(SystemExit, match=r"wan22.*missing segment 3"):
        segments_by_model(state, 4)


def test_postprocess_rejects_a_segment_older_than_the_one_it_continues():
    old_three = seg_record(3, "s3-old.mp4")
    state = [seg_record(1, "s1.mp4"), old_three, seg_record(2, "s2-new.mp4")]
    with pytest.raises(SystemExit, match="wan22 segment 3 was generated before segment 2"):
        segments_by_model(state, 3)


def test_postprocess_rejects_a_segment_that_started_from_another_frame():
    state = [seg_record(1, "s1.mp4"), {**seg_record(2, "s2.mp4"), "start_image": "elsewhere.png"}]
    with pytest.raises(SystemExit, match=r"wan22 segment 2 started from elsewhere\.png"):
        segments_by_model(state, 2)


def test_run_model_chain_records_the_start_image_and_invalidates_downstream(spec, tmp_path, monkeypatch):
    monkeypatch.setattr(run_chain, "REPO_DIR", tmp_path)
    stage = tmp_path / "outputs" / spec["name"]
    stage.mkdir(parents=True)
    (stage / "wan22_seg1_last.png").write_bytes(b"png")
    old = [seg_record(n, f"old{n}.mp4") for n in (1, 2, 3, 4)]
    (stage / "chain_state.json").write_text(json.dumps(old))
    monkeypatch.setattr(run_chain, "run_segment", lambda _spec, _model, seg: tmp_path / f"new{seg}.mp4")

    def fake_extract(_video, out_png):
        out_png.write_bytes(b"png")
        if out_png.name == "wan22_seg3_last.png":
            raise RuntimeError("segment 3 frame extraction failed")

    monkeypatch.setattr(run_chain, "extract_last_frame", fake_extract)
    with pytest.raises(RuntimeError, match="segment 3"):
        run_chain.run_model_chain({**spec, "spec_dir": tmp_path}, "wan22", 2)
    state = json.loads((stage / "chain_state.json").read_text())
    assert [(r["segment"], r["raw"]) for r in state] == [(1, "old1.mp4"), (2, "new2.mp4")]
    assert state[1]["start_image"] == "outputs/headphones_test/wan22_seg1_last.png"


def test_concat_list_uses_absolute_paths(tmp_path):
    assert concat_list([tmp_path / "a.mp4"]) == f"file '{tmp_path / 'a.mp4'}'\n"


def test_concat_list_escapes_apostrophes():
    assert concat_list([Path("/tmp/author's-repo/a.mp4")]) == "file '/tmp/author'\\''s-repo/a.mp4'\n"


def test_voiceover_is_padded_so_the_video_sets_the_length():
    mux = mux_command(Path("v.mp4"), Path("vo.mp3"), Path("o.mp4"), 20.25)
    assert mux[mux.index("-af") + 1] == "apad=whole_dur=20.250000"
    assert "-shortest" in mux
    compare = compare_command(Path("w.mp4"), Path("h.mp4"), Path("c.mp4"), Path("vo.mp3"), 20.25)
    assert compare[compare.index("-filter_complex") + 1].endswith(";[2:a]apad=whole_dur=20.250000[a]")
    assert compare[compare.index("-map", compare.index("[v]")) + 1] == "[a]"


def test_model_clips_split_the_chain_and_offset_the_voiceover(tmp_path):
    commands = []
    parts = [tmp_path / f"s{i}.mp4" for i in range(1, 5)]
    measured = []

    def measure(video, _run):
        measured.append(video)
        return 10.125

    record = lambda cmd, **_k: commands.append(cmd)  # noqa: E731
    joined = build_model_clips("wan22", parts, tmp_path, Path("vo.mp3"), 5, record, measure=measure)
    assert joined == tmp_path / "wan22_joined_20s_v.mp4"
    assert measured == [tmp_path / f"wan22_{name}_v.mp4" for name in ("clip1_10s", "clip2_10s", "joined_20s")]
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
    assert clip2_mux[clip2_mux.index("-ss") + 1] == "10.125"


def test_duration_is_read_from_the_last_progress_block():
    stdout = "frame=40\nout_time_us=2500000\nprogress=continue\nframe=81\nout_time_us=5062500\nprogress=end\n"
    assert parse_progress_duration(stdout) == 5.0625


def test_missing_progress_output_is_an_error():
    with pytest.raises(RuntimeError, match="no duration"):
        parse_progress_duration("progress=end\n")


def test_duration_command_decodes_the_first_video_stream():
    cmd = duration_command(Path("seg.mp4"))
    assert cmd[cmd.index("-progress") + 1] == "pipe:1"
    assert cmd[cmd.index("-map") + 1] == "0:v:0"
    assert cmd[-3:] == ["-f", "null", "-"]
    assert "copy" not in cmd


def test_silent_model_clips_skip_the_mux(tmp_path):
    commands = []
    parts = [tmp_path / f"s{i}.mp4" for i in range(1, 5)]
    build_model_clips("wan22", parts, tmp_path, None, 5, lambda cmd, **_k: commands.append(cmd))
    assert all("concat" in cmd for cmd in commands)
    assert len(commands) == 3


def test_comparison_with_voiceover_maps_the_audio_track():
    cmd = compare_command(Path("w.mp4"), Path("h.mp4"), Path("c.mp4"), Path("vo.mp3"), 20.25)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.startswith("[0:v]scale=-2:720[l];[1:v]scale=-2:720[r];[l][r]hstack[v];[2:a]")
    assert "[a]" in cmd
    assert "-an" not in cmd


def test_silent_comparison_has_no_audio_track():
    cmd = compare_command(Path("w.mp4"), Path("h.mp4"), Path("c.mp4"), None)
    assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"] == ["w.mp4", "h.mp4"]
    assert "-an" in cmd
    assert "aac" not in cmd
