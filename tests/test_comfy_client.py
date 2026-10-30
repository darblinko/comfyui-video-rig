import pytest

import comfy_client
from comfy_client import ComfyClient, ComfyError


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(comfy_client.time, "sleep", lambda _s: None)
    instance = ComfyClient()
    instance.posts = []
    monkeypatch.setattr(instance, "_post_json", lambda path, payload, _ctx: instance.posts.append((path, payload)))
    monkeypatch.setattr(instance, "_get_json", lambda path: RUNNING_P1 if path == "/queue" else {})
    return instance


RUNNING_P1 = {"queue_running": [[1, "p1", {}, {}, []]], "queue_pending": []}


def history_sequence(client, monkeypatch, responses):
    def get_json(path):
        if path == "/queue":
            return RUNNING_P1
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(client, "_get_json", get_json)


def test_ping_reads_the_queue(client, monkeypatch):
    paths = []
    monkeypatch.setattr(client, "_get_json", lambda path: paths.append(path) or {})
    client.ping()
    assert paths == ["/queue"]


def test_wait_returns_the_finished_entry(client, monkeypatch):
    done = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}, "status": {"status_str": "success"}}
    history_sequence(client, monkeypatch, [{}, {"p1": {"outputs": {}}}, {"p1": done}])
    assert client.wait("p1") == done


def test_wait_raises_on_a_generation_error(client, monkeypatch):
    failed = {"status": {"status_str": "error", "messages": [["execution_error", {"node": "3"}]]}}
    history_sequence(client, monkeypatch, [{"p1": failed}])
    with pytest.raises(ComfyError, match="generation failed"):
        client.wait("p1")


def test_wait_survives_four_poll_failures(client, monkeypatch):
    done = {"outputs": {"9": {}}}
    history_sequence(client, monkeypatch, [ComfyError("blip")] * 4 + [{"p1": done}])
    assert client.wait("p1") == done
    assert client.posts == []


def test_fifth_poll_failure_cancels_and_raises(client, monkeypatch):
    history_sequence(client, monkeypatch, [ComfyError("tunnel closed")] * 5)
    with pytest.raises(ComfyError, match="tunnel closed"):
        client.wait("p1")
    assert client.posts == [("/queue", {"delete": ["p1"]}), ("/interrupt", {"prompt_id": "p1"})]


def test_timeout_cancels_the_prompt(client):
    with pytest.raises(ComfyError, match="timed out"):
        client.wait("p1", timeout_s=0)
    assert [path for path, _ in client.posts] == ["/queue", "/interrupt"]


def test_output_files_collects_every_media_kind():
    entry = {
        "outputs": {
            "1": {"images": [{"filename": "a.png"}]},
            "2": {"gifs": [{"filename": "b.mp4"}], "text": ["ignored"]},
            "3": {"videos": [{"filename": ""}, {"filename": "c.webm"}]},
        }
    }
    assert [f["filename"] for f in ComfyClient.output_files(entry)] == ["a.png", "b.mp4", "c.webm"]
