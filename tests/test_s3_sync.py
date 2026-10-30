import s3_sync
from conftest import TEST_BUCKET, FakeSession


def test_sync_uploads_every_file_and_presigns_media(tmp_path, monkeypatch):
    run_dir = tmp_path / "20260711-120000-demo"
    (run_dir / "wan22").mkdir(parents=True)
    (run_dir / "final_wan22.mp4").write_bytes(b"v")
    (run_dir / "wan22" / "frame.png").write_bytes(b"i")
    (run_dir / "notes.txt").write_text("n")
    session = FakeSession(
        {"s3.generate_presigned_url": lambda *a, **k: f"https://example.invalid/{k['Params']['Key']}"}
    )
    monkeypatch.setattr(s3_sync, "aws_session", lambda _region: session)

    urls = s3_sync.sync_run(run_dir, {"region": "us-west-2", "bucket": TEST_BUCKET})

    uploads = session.kwargs_of("s3.upload_file")
    assert [call["arg2"] for call in uploads] == [
        "runs/20260711-120000-demo/final_wan22.mp4",
        "runs/20260711-120000-demo/notes.txt",
        "runs/20260711-120000-demo/wan22/frame.png",
    ]
    assert uploads[0]["ExtraArgs"] == {"ContentType": "video/mp4"}
    assert sorted(urls) == ["final_wan22.mp4", "wan22/frame.png"]
    assert all(kwargs["ExpiresIn"] == 7 * 24 * 3600 for kwargs in session.kwargs_of("s3.generate_presigned_url"))
