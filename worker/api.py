"""HTTP client for the server's /api/worker routes (stdlib only)."""

import json
import secrets
import urllib.error
import urllib.request
from pathlib import Path


class ApiError(Exception):
    def __init__(self, status: int, body: bytes | str = b""):
        self.status = status
        self.body = body
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        super().__init__(f"HTTP {status}: {text[:300]}")


def urllib_transport(method, url, headers, body):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def build_multipart(fields: dict, files: list[tuple[str, str, bytes]]):
    """files: (field name, file name, bytes). Returns (content_type, body)."""
    boundary = "----autojob" + secrets.token_hex(16)
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            + str(value).encode("utf-8")
            + b"\r\n"
        )
    for key, filename, data in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
            f'filename="{filename}"\r\nContent-Type: image/png\r\n\r\n'.encode()
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


class Api:
    def __init__(self, base_url: str, token: str, transport=urllib_transport):
        self.base = base_url.rstrip("/") + "/api/worker"
        self.token = token
        self.transport = transport

    def _call(self, method, path, *, json_body=None, raw=None, content_type=None):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            body = raw
            headers["Content-Type"] = content_type
        status, data = self.transport(method, self.base + path, headers, body)
        if status >= 400:
            raise ApiError(status, data)
        return status, data

    def claim(self, hostname: str):
        status, data = self._call("POST", "/claim", json_body={"worker": hostname})
        if status == 204 or not data:
            return None
        return json.loads(data)

    def download_cv(self, dest) -> Path:
        _, data = self._call("GET", "/cv")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def heartbeat(self, job_id: int, lease_token: str) -> dict:
        _, data = self._call(
            "POST", f"/jobs/{job_id}/heartbeat", json_body={"lease_token": lease_token}
        )
        return json.loads(data)

    def post_attempt(self, job_id: int, lease_token: str, payload: dict, screenshot_paths) -> dict:
        files = []
        for path in screenshot_paths:
            p = Path(path)
            files.append(("screenshots", p.name, p.read_bytes()))
        ctype, body = build_multipart(
            {"lease_token": lease_token, "payload": json.dumps(payload)}, files
        )
        _, data = self._call(
            "POST", f"/jobs/{job_id}/attempts", raw=body, content_type=ctype
        )
        return json.loads(data)

    def release(self, job_id: int, lease_token: str, reason: str) -> None:
        self._call(
            "POST",
            f"/jobs/{job_id}/release",
            json_body={"lease_token": lease_token, "reason": reason},
        )
