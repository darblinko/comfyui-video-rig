"""Minimal ComfyUI HTTP API client (stdlib only).

Talks to a ComfyUI instance, normally through the SSM tunnel opened by
`python3 launch_rig.py tunnel` (localhost:8188).
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEFAULT_POLL_S = 5


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8188, timeout: int = 60):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout
        self.client_id = uuid.uuid4().hex

    def _request(self, req: urllib.request.Request | str, context: str, read):
        """Open req and return read(response). Every failure, including a timeout
        or a broken connection while the body is read and a body that is not
        JSON, is raised as ComfyError, so the runner's per-run error handling
        (manifest, S3 sync) always survives."""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return read(resp)
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            raise ComfyError(f"{context} ({err.code}): {detail[:2000]}") from err
        except json.JSONDecodeError as err:
            raise ComfyError(f"{context}: response is not valid JSON ({err})") from err
        except (urllib.error.URLError, http.client.HTTPException, OSError) as err:
            raise ComfyError(f"{context}: cannot reach ComfyUI at {self.base}; is the tunnel open? ({err})") from err

    def _post_json(self, path: str, payload: dict, context: str) -> None:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        self._request(req, context, lambda resp: resp.read())

    def _get_json(self, path: str) -> dict:
        return self._request(self.base + path, f"GET {path} failed", json.load)

    def ping(self) -> None:
        """Raise ComfyError unless the server answers; run_job calls this before
        it creates a run folder, so a closed tunnel leaves nothing behind."""
        self._get_json("/queue")

    def queue_prompt(self, workflow: dict) -> str:
        payload = json.dumps({"prompt": workflow, "client_id": self.client_id}).encode()
        req = urllib.request.Request(
            self.base + "/prompt",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        data = self._request(req, "ComfyUI rejected the workflow", json.load)
        return data["prompt_id"]

    def cancel(self, prompt_id: str) -> None:
        """Best-effort, and only for this prompt: drop it from the pending queue,
        then interrupt it if it is the one running. Keeps a timed-out job from
        grinding (and the idle watchdog from seeing a busy queue) after the
        client gives up, without aborting another client's running prompt."""
        try:
            self._post_json("/queue", {"delete": [prompt_id]}, "POST /queue failed")
        except ComfyError as err:
            print(f"  cancel via /queue: {err}")
        try:
            running = self._get_json("/queue").get("queue_running", [])
        except ComfyError as err:
            print(f"  cancel: queue unreadable, not interrupting: {err}")
            return
        if not any(len(item) > 1 and item[1] == prompt_id for item in running):
            return
        try:
            # Current ComfyUI interrupts only the named prompt when prompt_id is given.
            self._post_json("/interrupt", {"prompt_id": prompt_id}, "POST /interrupt failed")
        except ComfyError as err:
            print(f"  cancel via /interrupt: {err}")

    def wait(self, prompt_id: str, timeout_s: int = 3600, poll_s: int = DEFAULT_POLL_S) -> dict:
        """Poll until the prompt finishes; returns its history entry.

        Transient poll failures (tunnel blips) are retried; on timeout the
        prompt is cancelled server-side before raising, so a dead client
        cannot leave the GPU busy on a job nobody is watching."""
        started = time.monotonic()
        last_note = started
        poll_failures = 0
        while time.monotonic() - started < timeout_s:
            try:
                hist = self._get_json(f"/history/{prompt_id}")
                poll_failures = 0
            except ComfyError as err:
                poll_failures += 1
                if poll_failures >= 5:
                    self.cancel(prompt_id)
                    raise
                print(f"  ... poll failed, retrying: {err}")
                time.sleep(poll_s)
                continue
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    messages = json.dumps(status.get("messages", []))
                    raise ComfyError(f"generation failed: {messages[:2000]}")
                if entry.get("outputs"):
                    return entry
            if time.monotonic() - last_note >= 60:
                elapsed = int(time.monotonic() - started)
                print(f"  ... still generating ({elapsed}s elapsed)")
                last_note = time.monotonic()
            time.sleep(poll_s)
        self.cancel(prompt_id)
        raise ComfyError(f"timed out after {timeout_s}s waiting for prompt {prompt_id} (cancellation sent)")

    def upload_image(self, path: Path) -> str:
        """Upload an input image under a unique name; returns the server-side
        path to reference, so two inputs with the same basename never collide."""
        name = f"{uuid.uuid4().hex[:12]}_{path.name}"
        boundary = uuid.uuid4().hex
        parts = [
            f"--{boundary}\r\n".encode(),
            (f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n').encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            path.read_bytes(),
            f"\r\n--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
            f"--{boundary}--\r\n".encode(),
        ]
        req = urllib.request.Request(
            self.base + "/upload/image",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        data = self._request(req, f"upload of {path.name} failed", json.load)
        subfolder = data.get("subfolder") or ""
        return f"{subfolder}/{data['name']}" if subfolder else data["name"]

    def download_output(self, file_info: dict, dest_dir: Path) -> Path:
        """Stream one output to dest_dir. The body goes to a .part file that is
        renamed only when complete, so a failed download leaves no file."""
        query = urllib.parse.urlencode(
            {
                "filename": file_info["filename"],
                "subfolder": file_info.get("subfolder", ""),
                "type": file_info.get("type", "output"),
            }
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(file_info["filename"]).name
        partial = dest.with_name(dest.name + ".part")

        def save(resp) -> None:
            with open(partial, "wb") as out:
                while chunk := resp.read(1 << 20):
                    out.write(chunk)

        try:
            self._request(f"{self.base}/view?{query}", f"download of {file_info['filename']} failed", save)
        except ComfyError:
            partial.unlink(missing_ok=True)
            raise
        partial.replace(dest)
        return dest

    @staticmethod
    def output_files(history_entry: dict) -> list[dict]:
        files = []
        for node_output in history_entry.get("outputs", {}).values():
            for key in ("images", "gifs", "videos", "video", "audio"):
                for item in node_output.get(key) or []:
                    if isinstance(item, dict) and item.get("filename"):
                        files.append(item)
        return files
