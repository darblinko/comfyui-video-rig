"""ComfyClient against a local HTTP server that speaks the ComfyUI routes the rig uses.

These tests exercise request encoding, response parsing and failures while a
response body is being read, which the stubbed tests in test_comfy_client.py
do not reach."""

from __future__ import annotations

import json
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from comfy_client import ComfyClient, ComfyError

CLIENT_TIMEOUT_S = 1


class FakeComfyServer:
    """Records each request and answers from per-route settings."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.queue = {"queue_running": [], "queue_pending": []}
        self.stall = set()
        self.release = threading.Event()
        self.raw_bodies: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.upload_subfolder = ""
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                server.handle(self, "GET")

            def do_POST(self):
                server.handle(self, "POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def close(self) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        path = handler.path.split("?")[0]
        self.requests.append((method, handler.path, dict(handler.headers), body))
        if path == "/upload/image":
            payload = json.dumps(self.store_upload(handler.headers["Content-Type"], body)).encode()
        elif path in self.raw_bodies:
            payload = self.raw_bodies[path]
        elif path == "/prompt":
            payload = json.dumps({"prompt_id": "p1", "number": 1}).encode()
        elif path == "/queue" and method == "GET":
            payload = json.dumps(self.queue).encode()
        elif path.startswith("/history/"):
            payload = json.dumps({"p1": {"outputs": {"9": {}}}}).encode()
        elif path == "/view":
            payload = b"x" * 4096
        else:
            payload = b""
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        if path in self.stall:
            handler.wfile.write(payload[:10])
            handler.wfile.flush()
            self.release.wait(CLIENT_TIMEOUT_S * 3)
            return
        handler.wfile.write(payload)

    def store_upload(self, content_type: str, body: bytes) -> dict:
        message = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
        fields = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
        filename = fields["image"].get_filename()
        self.uploads.append(filename)
        return {"name": filename, "subfolder": self.upload_subfolder, "type": "input"}

    def posts_to(self, path: str) -> list[dict]:
        return [json.loads(body or b"{}") for method, p, _h, body in self.requests if method == "POST" and p == path]


@pytest.fixture
def server():
    fake = FakeComfyServer()
    yield fake
    fake.close()


@pytest.fixture
def client(server) -> ComfyClient:
    return ComfyClient(port=server.port, timeout=CLIENT_TIMEOUT_S)


def test_queue_prompt_posts_the_workflow_as_json(server, client):
    workflow = {"3": {"class_type": "KSampler", "inputs": {"seed": 7}}}
    assert client.queue_prompt(workflow) == "p1"
    method, path, headers, body = server.requests[-1]
    assert (method, path, headers["Content-Type"]) == ("POST", "/prompt", "application/json")
    assert json.loads(body) == {"prompt": workflow, "client_id": client.client_id}


def test_http_error_detail_is_reported(client, monkeypatch, server):
    def reject(handler, _method):
        handler.send_response(400)
        handler.end_headers()
        handler.wfile.write(b'{"error": "invalid prompt"}')

    monkeypatch.setattr(server, "handle", reject)
    with pytest.raises(ComfyError, match="invalid prompt"):
        client.queue_prompt({})


def test_timeout_while_reading_json_becomes_a_comfy_error(server, client):
    server.stall.add("/history/p1")
    with pytest.raises(ComfyError, match="GET /history/p1"):
        client._get_json("/history/p1")


def test_invalid_json_becomes_a_comfy_error(server, client):
    server.raw_bodies["/queue"] = b"<html>proxy error</html>"
    with pytest.raises(ComfyError, match="not valid JSON"):
        client.ping()


def test_timeout_during_download_removes_the_partial_file(server, client, tmp_path):
    server.stall.add("/view")
    with pytest.raises(ComfyError, match=r"download of clip\.mp4"):
        client.download_output({"filename": "clip.mp4", "subfolder": "", "type": "output"}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_writes_the_whole_body(server, client, tmp_path):
    dest = client.download_output({"filename": "clip.mp4", "subfolder": "rig", "type": "output"}, tmp_path)
    assert dest.read_bytes() == b"x" * 4096
    assert "filename=clip.mp4&subfolder=rig&type=output" in server.requests[-1][1]
