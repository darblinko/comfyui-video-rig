import subprocess
from pathlib import Path

from media import portrait_encode_command, run_ffmpeg, side_by_side_command


def test_portrait_encode_fits_and_pads_to_9_16():
    cmd = portrait_encode_command(Path("in.webp"), Path("out.mp4"), [720, 1280])
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2"
    assert cmd[:4] == ["ffmpeg", "-y", "-i", "in.webp"]
    assert cmd[-1] == "out.mp4"
    for flag in ("libx264", "yuv420p", "+faststart"):
        assert flag in cmd


def test_side_by_side_needs_two_clips():
    assert side_by_side_command([Path("a.mp4")], Path("c.mp4")) is None


def test_side_by_side_stacks_the_first_two_clips_at_720p():
    cmd = side_by_side_command([Path("a.mp4"), Path("b.mp4"), Path("ignored.mp4")], Path("c.mp4"))
    assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"] == ["a.mp4", "b.mp4"]
    assert cmd[cmd.index("-filter_complex") + 1] == "[0:v]scale=-2:720[l];[1:v]scale=-2:720[r];[l][r]hstack"
    assert cmd[-1] == "c.mp4"


def test_run_ffmpeg_returns_the_output_on_success(tmp_path):
    calls = []
    dest = tmp_path / "out.mp4"
    assert run_ffmpeg(["ffmpeg"], dest, run=lambda cmd, **kw: calls.append(cmd)) == dest
    assert calls == [["ffmpeg"]]


def test_run_ffmpeg_failure_removes_the_partial_file(tmp_path, capsys):
    dest = tmp_path / "out.mp4"
    dest.write_bytes(b"partial")

    def fail(cmd, **_kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr=b"line 1\nInvalid data found\n")

    assert run_ffmpeg(["ffmpeg"], dest, run=fail) is None
    assert not dest.exists()
    assert "Invalid data found" in capsys.readouterr().out
