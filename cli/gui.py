"""Local web GUI for clouddupe: ``python3 clouddupe.py gui``.

Serves the same interface as the desktop app (built into ``cli/web``) on
127.0.0.1 and drives the installed rclone. Requests need the random token in
the URL the browser is opened with, and the Host header must be the loopback
address, so other sites and other users on the network cannot use it.
"""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import clouddupe as cd

WEB_DIR = Path(__file__).resolve().parent / "web"
HIDDEN_PROVIDERS = {"alias", "archive", "cache", "chunker", "combine", "compress",
                    "crypt", "hasher", "memory", "union"}
NAME_RE = re.compile(r"^[^\W][\w.+@ -]*$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9_]+$")
EXPORT_TYPES = {"html": "text/html; charset=utf-8", "csv": "text/csv; charset=utf-8",
                "json": "application/json"}


class ApiError(Exception):
    """Shown to the user as-is."""


def error_message(stderr: str, code: int) -> str:
    lines = [l for l in stderr.splitlines() if l.strip()]
    line = next((l for l in reversed(lines)
                 if "ERROR" in l or "Fatal error" in l or "Failed" in l),
                lines[-1] if lines else "")
    for marker in ("Fatal error: ", "ERROR : "):
        if marker in line:
            line = line.split(marker, 1)[1]
            break
    return line.strip() or f"rclone exited with code {code}"


def validate_name(name: str) -> None:
    if not (0 < len(name) <= 64) or name.endswith(" ") or name.startswith("-") \
            or not NAME_RE.match(name) or "/" in name:
        raise ApiError("Use letters, numbers, spaces and _ - . + @ only, "
                       "not starting with - or a space.")


def parse_step(stdout: str) -> dict:
    text = stdout.strip()
    if not text:
        return {"done": True, "state": "", "question": None, "error": "", "oauth": False}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ApiError(f"unexpected rclone output ({exc})") from exc
    state = data.get("State") or ""
    error = data.get("Error") or ""
    if not state:
        return {"done": True, "state": "", "question": None, "error": error, "oauth": False}
    o = data.get("Option") or {}
    default = o.get("DefaultStr")
    if default is None:
        d = o.get("Default")
        default = "" if d is None else (d if isinstance(d, str) else json.dumps(d))
    question = {
        "name": o.get("Name") or "",
        "help": o.get("Help") or "",
        "default": default,
        "examples": [{"value": str(e.get("Value", "")), "help": e.get("Help") or ""}
                     for e in (o.get("Examples") or [])],
        "required": bool(o.get("Required")),
        "isPassword": bool(o.get("IsPassword")),
        "kind": o.get("Type") or "string",
        "exclusive": bool(o.get("Exclusive")),
    }
    return {"done": False, "state": state, "question": question, "error": error,
            "oauth": question["name"] == "config_is_local"}


class Rclone:
    def __init__(self, binary: str) -> None:
        self.binary = binary
        self._lock = threading.Lock()
        self._config_proc: subprocess.Popen | None = None

    def run(self, args: list[str], stdin: str | None = None,
            track: bool = False) -> subprocess.CompletedProcess:
        proc = subprocess.Popen(
            [self.binary, *args],
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        if track:
            with self._lock:
                self._config_proc = proc
        try:
            out, err = proc.communicate(stdin)
        finally:
            if track:
                with self._lock:
                    cancelled = self._config_proc is None
                    self._config_proc = None
                if cancelled:
                    raise ApiError("cancelled")
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def checked(self, args: list[str], **kw: Any) -> str:
        res = self.run(args, **kw)
        if res.returncode != 0:
            raise ApiError(error_message(res.stderr, res.returncode))
        return res.stdout

    def cancel_config(self) -> None:
        with self._lock:
            proc, self._config_proc = self._config_proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()


class ScanJob:
    def __init__(self, rclone: Rclone, request: dict) -> None:
        self.rclone = rclone
        self.request = request
        self.events: list[dict] = []
        self.done = False
        self.result: dict | None = None
        self.error: str | None = None
        self.summary: dict | None = None
        self.groups: list[cd.Group] = []
        self._cancel = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()

    def _emit(self, target: str, files: int, done: bool, error: str | None = None) -> None:
        with self._lock:
            self.events.append({"target": target, "files": files, "done": done, "error": error})

    def _list(self, target: str, files: list[cd.FileEntry]) -> None:
        cmd = [self.rclone.binary, "lsjson", "-R", "--files-only", "--hash", "--no-mimetype", target]
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                    text=True, encoding="utf-8")
            with self._lock:
                self._proc = proc
            count = 0
            assert proc.stdout is not None
            try:
                for item in cd.iter_lsjson_lines(proc.stdout):
                    entry = cd.parse_item(item, target, len(files))
                    if entry is not None:
                        files.append(entry)
                        count += 1
                        if count % 250 == 0:
                            self._emit(target, count, False)
            except json.JSONDecodeError as exc:
                proc.kill()
                if not self._cancel.is_set():
                    raise ApiError(f"{target}: could not parse rclone output ({exc})") from exc
            finally:
                proc.stdout.close()
            code = proc.wait()
            with self._lock:
                self._proc = None
            if self._cancel.is_set():
                raise ApiError("cancelled")
            if code != 0:
                err.seek(0)
                raise ApiError(f"{target}: {error_message(err.read(), code)}")
            self._emit(target, count, True)

    def _run(self) -> None:
        try:
            files: list[cd.FileEntry] = []
            errors: list[str] = []
            for target in self.request["targets"]:
                if self._cancel.is_set():
                    raise ApiError("cancelled")
                self._emit(target, 0, False)
                try:
                    self._list(target, files)
                except ApiError as exc:
                    if str(exc) == "cancelled":
                        raise
                    self._emit(target, 0, True, str(exc))
                    errors.append(str(exc))
            groups = cd.detect(files, max(int(self.request.get("minSize", 1)), 0),
                               bool(self.request.get("sizeOnly")),
                               max(int(self.request.get("sizeOnlyMin", 0)), 0))
            summary = cd.summarise(files, groups, errors)
            summary["generated_at"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
            self.groups, self.summary = groups, summary
            self.result = {
                "summary": summary,
                "groups": [{"id": g.gid, "confidence": g.confidence, "evidence": g.evidence,
                            "warnings": g.warnings,
                            "members": [{"idx": m.idx, "remote": m.remote, "path": m.path,
                                         "size": m.size, "mod_time": m.mod_time, "hashes": m.hashes}
                                        for m in g.members]}
                           for g in groups],
            }
        except ApiError as exc:
            self.error = str(exc)
        except Exception as exc:  # surface unexpected failures in the UI
            self.error = f"scan failed: {exc}"
        finally:
            self.done = True


class Api:
    def __init__(self, rclone: Rclone) -> None:
        self.rclone = rclone
        self.job: ScanJob | None = None

    def rclone_version(self, _: dict) -> str:
        return self.rclone.checked(["version"]).splitlines()[0]

    def list_remotes(self, _: dict) -> list:
        items = json.loads(self.rclone.checked(["listremotes", "--json"]) or "[]")
        return [{"name": i.get("name", ""), "type": i.get("type", ""),
                 "description": i.get("description", "")} for i in items if i.get("name")]

    def list_providers(self, _: dict) -> list:
        items = json.loads(self.rclone.checked(["config", "providers"]))
        out = [{"name": p["Name"], "description": p.get("Description", "")}
               for p in items if not p.get("Hide") and p["Name"] not in HIDDEN_PROVIDERS]
        return sorted(out, key=lambda p: p["description"].lower())

    def delete_remote(self, body: dict) -> None:
        name = str(body.get("name", ""))
        validate_name(name)
        self.rclone.checked(["config", "delete", name])

    def _continue(self, name: str, state: str, result: str) -> dict:
        out = self.rclone.checked(["config", "update", name, "--continue", "--non-interactive",
                                   "--all", "--state", state, "--result", result], track=True)
        return parse_step(out)

    def _auto(self, name: str, step: dict) -> dict:
        while step["question"] is not None and step["question"]["name"] == "config_fs_advanced":
            step = self._continue(name, step["state"], "false")
        return step

    def config_start(self, body: dict) -> dict:
        name, provider = str(body.get("name", "")), str(body.get("provider", ""))
        validate_name(name)
        if not PROVIDER_RE.match(provider):
            raise ApiError("invalid provider")
        if any(r["name"] == name for r in self.list_remotes({})):
            raise ApiError(f'A drive called "{name}" already exists.')
        try:
            out = self.rclone.checked(["config", "create", name, provider, "--non-interactive", "--all"],
                                      track=True)
            return self._auto(name, parse_step(out))
        except ApiError:
            # rclone saves the remote as soon as create starts; remove the half-made entry.
            self.rclone.run(["config", "delete", name])
            raise

    def config_answer(self, body: dict) -> dict:
        name = str(body.get("name", ""))
        validate_name(name)
        result = str(body.get("result", ""))
        if body.get("isPassword") and result:
            # rclone stores --continue passwords as given; obscure first, via stdin.
            result = self.rclone.checked(["obscure", "-"], stdin=result).strip()
        return self._auto(name, self._continue(name, str(body.get("stepState", "")), result))

    def config_cancel(self, _: dict) -> None:
        self.rclone.cancel_config()

    def start_scan(self, body: dict) -> None:
        req = body.get("request") or {}
        targets = [str(t) for t in req.get("targets") or []]
        if not targets:
            raise ApiError("Choose at least one drive.")
        if any(t.startswith("-") or ":" not in t for t in targets):
            raise ApiError("invalid target")
        if self.job is not None and not self.job.done:
            raise ApiError("A scan is already running.")
        self.job = ScanJob(self.rclone, {**req, "targets": targets})

    def scan_poll(self, body: dict) -> dict:
        job = self.job
        if job is None:
            raise ApiError("No scan running.")
        since = max(int(body.get("since", 0)), 0)
        with job._lock:
            events = job.events[since:]
        done = job.done
        return {"events": events, "done": done,
                "result": job.result if done else None,
                "error": job.error if done else None}

    def cancel_scan(self, _: dict) -> None:
        if self.job is not None:
            self.job.cancel()

    def export(self, fmt: str) -> bytes:
        job = self.job
        if job is None or job.summary is None:
            raise ApiError("Run a scan first.")
        writers = {"html": cd.write_html, "csv": cd.write_csv, "json": cd.write_json}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            if fmt == "csv":
                path = cd.write_csv(out, job.groups)
            else:
                path = writers[fmt](out, job.summary, job.groups)
            return path.read_bytes()


def make_handler(api: Api, token: str, port_ref: list[int]):
    endpoints = {"rclone_version", "list_remotes", "list_providers", "delete_remote",
                 "config_start", "config_answer", "config_cancel", "start_scan",
                 "scan_poll", "cancel_scan"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "clouddupe"

        def log_message(self, *args: Any) -> None:  # keep the terminal quiet
            pass

        def _host_ok(self) -> bool:
            host = self.headers.get("Host", "")
            return host in (f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}")

        def _token_ok(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Clouddupe-Token", ""), token)

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def do_GET(self) -> None:
            if not self._host_ok():
                self._json(403, {"ok": False, "error": "forbidden host"})
                return
            url = urlparse(self.path)
            if url.path == "/api/export":
                if not self._token_ok():
                    self._json(403, {"ok": False, "error": "forbidden"})
                    return
                fmt = parse_qs(url.query).get("format", [""])[0]
                if fmt not in EXPORT_TYPES:
                    self._json(400, {"ok": False, "error": "unknown format"})
                    return
                try:
                    body = api.export(fmt)
                except ApiError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                self._send(200, body, EXPORT_TYPES[fmt],
                           {"Content-Disposition": f'attachment; filename="clouddupe-report.{fmt}"'})
                return
            rel = url.path.lstrip("/") or "index.html"
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in target.parents or not target.is_file():
                self._json(404, {"ok": False, "error": "not found"})
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/javascript":
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self) -> None:
            url = urlparse(self.path)
            name = url.path.removeprefix("/api/")
            if not self._host_ok() or not self._token_ok():
                self._json(403, {"ok": False, "error": "forbidden"})
                return
            if name not in endpoints:
                self._json(404, {"ok": False, "error": "unknown endpoint"})
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"ok": False, "error": "expected JSON"})
                return
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
                body = json.loads(self.rfile.read(length) or b"{}")
                result = getattr(api, name)(body if isinstance(body, dict) else {})
                self._json(200, {"ok": True, "result": result})
            except ApiError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            except (ValueError, KeyError, OSError) as exc:
                self._json(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return Handler


def serve(rclone_binary: str, port: int = 0, open_browser: bool = True) -> int:
    if not (WEB_DIR / "index.html").is_file():
        print(f"error: web interface not found in {WEB_DIR}", file=sys.stderr)
        return 2
    token = secrets.token_urlsafe(24)
    port_ref = [port]
    api = Api(Rclone(rclone_binary))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api, token, port_ref))
    port_ref[0] = server.server_address[1]
    url = f"http://127.0.0.1:{port_ref[0]}/#token={token}"
    print(f"Cloud Dupe is running at {url}\nPress Ctrl+C to stop.", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        if api.job is not None:
            api.job.cancel()
        api.rclone.cancel_config()
        server.server_close()
    return 0
