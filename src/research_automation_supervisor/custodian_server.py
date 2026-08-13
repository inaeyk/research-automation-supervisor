"""Loopback-only progressive-disclosure web UI for the Campaign Custodian."""

# The embedded, minified browser asset intentionally keeps CSS/JS lines intact.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError

from research_automation_supervisor import __version__
from research_automation_supervisor.core_authority_client import DEFAULT_CORE_SOCKET
from research_automation_supervisor.custodian import (
    CampaignCustodian,
    WizardSubmissionV1,
)
from research_automation_supervisor.custodian_errors import CustodianError, CustodianStateError
from research_automation_supervisor.custodian_models import (
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
)
from research_automation_supervisor.durable_state import atomic_write_json

MAX_REQUEST_BYTES = 64 * 1024 * 1024
CAMPAIGN_ROUTE = re.compile(r"^/api/campaigns/(?P<campaign>campaign-[a-z0-9-]+)$")
REQUEST_ROUTE = re.compile(r"^/api/campaigns/(?P<campaign>campaign-[a-z0-9-]+)/request$")
CONTINUE_ROUTE = re.compile(r"^/api/campaigns/(?P<campaign>campaign-[a-z0-9-]+)/continue$")
RESPOND_ROUTE = re.compile(r"^/api/campaigns/(?P<campaign>campaign-[a-z0-9-]+)/respond$")
EXPORT_ROUTE = re.compile(r"^/api/campaigns/(?P<campaign>campaign-[a-z0-9-]+)/export$")
OPEN_ROUTE = re.compile(r"^/api/campaigns/(?P<campaign>campaign-[a-z0-9-]+)/open$")
ARTIFACT_ROUTE = re.compile(
    r"^/api/campaigns/(?P<campaign>campaign-[a-z0-9-]+)/artifacts/(?P<token>[a-f0-9]{16,80})$"
)
DOWNLOAD_ROUTE = re.compile(r"^/api/campaigns/(?P<campaign>campaign-[a-z0-9-]+)/download$")


class CustodianHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        custodian: CampaignCustodian,
        *,
        session_secret: str,
        readiness_instance: str | None = None,
    ) -> None:
        self.custodian = custodian
        self.session_secret = session_secret
        self.csrf_token = hashlib.sha256(f"csrf:{session_secret}".encode("ascii")).hexdigest()
        self.readiness_instance = readiness_instance or secrets.token_hex(32)
        super().__init__(server_address, CustodianRequestHandler)


class CustodianRequestHandler(BaseHTTPRequestHandler):
    server: CustodianHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "ResearchSupervisorCustodian"
    sys_version = ""
    _security_headers: ClassVar[dict[str, str]] = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cache-Control": "no-store",
        "Cross-Origin-Resource-Policy": "same-origin",
    }

    def do_GET(self) -> None:
        try:
            self._validate_host()
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._html_response()
                return
            if path == "/api/health":
                self._json_response(
                    {
                        "ready": True,
                        "application": "Research Automation Supervisor",
                        "version": __version__,
                        "qualified_commit": os.environ.get("RAS_QUALIFIED_COMMIT", "development"),
                        "readiness_instance": self.server.readiness_instance,
                    }
                )
                return
            self._require_session()
            if path == "/api/environment":
                self._json_response(self.server.custodian.environment().model_dump(mode="json"))
                return
            if path == "/api/campaigns":
                campaigns = self.server.custodian.list_campaigns(refresh=True)
                self._json_response(
                    {"campaigns": [item.model_dump(mode="json") for item in campaigns]}
                )
                return
            match = CAMPAIGN_ROUTE.fullmatch(path)
            if match:
                record = self.server.custodian.get_record(match.group("campaign"), refresh=True)
                self._json_response(record.projection.model_dump(mode="json"))
                return
            match = REQUEST_ROUTE.fullmatch(path)
            if match:
                request = self.server.custodian.request(match.group("campaign"))
                self._json_response(request.model_dump(mode="json"))
                return
            match = ARTIFACT_ROUTE.fullmatch(path)
            if match:
                media_type, content = self.server.custodian.read_artifact(
                    match.group("campaign"), match.group("token")
                )
                self._bytes_response(media_type, content)
                return
            match = DOWNLOAD_ROUTE.fullmatch(path)
            if match:
                export = self.server.custodian.exports / f"{match.group('campaign')}.zip"
                content = _read_download(export)
                self._bytes_response(
                    "application/zip",
                    content,
                    disposition=f'attachment; filename="{export.name}"',
                )
                return
            self._error_response(HTTPStatus.NOT_FOUND, "Page not found", "not_found")
        except Exception as exc:
            self._handle_exception(exc)

    def do_POST(self) -> None:
        try:
            self._validate_host()
            self._require_session()
            self._require_csrf()
            path = self.path.split("?", 1)[0]
            if path == "/api/preview":
                submission = _wizard_submission(self._read_json_body())
                preview = self.server.custodian.preview(submission)
                self._json_response(preview.model_dump(mode="json"))
                return
            if path == "/api/start":
                body = self._read_json_body()
                preview_id = _required_string(body, "preview_id")
                start_key = _required_string(body, "start_key")
                record = self.server.custodian.start(preview_id, client_start_key=start_key)
                self._json_response(record.projection.model_dump(mode="json"))
                return
            if path == "/api/sign-in":
                pid = self.server.custodian.sign_in()
                self._json_response(
                    {
                        "started": True,
                        "message": (
                            "The approved Codex sign-in flow has started. Complete it in "
                            "the browser, then choose Continue."
                        ),
                        "local_process": pid,
                    }
                )
                return
            if path == "/api/pick-folder":
                selected_folder = _pick_repository_folder()
                self._json_response({"path": selected_folder})
                return
            match = CONTINUE_ROUTE.fullmatch(path)
            if match:
                record = self.server.custodian.continue_campaign(match.group("campaign"))
                self._json_response(record.projection.model_dump(mode="json"))
                return
            match = RESPOND_ROUTE.fullmatch(path)
            if match:
                body = self._read_json_body()
                uploads = _response_uploads(body.get("uploads", []))
                selected_option = body.get("selected_option_id")
                note = body.get("response_text", "")
                if selected_option is not None and not isinstance(selected_option, str):
                    raise ValueError("selected response is invalid")
                if not isinstance(note, str):
                    raise ValueError("response note is invalid")
                record = self.server.custodian.respond(
                    match.group("campaign"),
                    selected_option_id=selected_option,
                    response_text=note,
                    uploads=uploads,
                )
                self._json_response(record.projection.model_dump(mode="json"))
                return
            match = EXPORT_ROUTE.fullmatch(path)
            if match:
                exported = self.server.custodian.export_campaign(match.group("campaign"))
                self._json_response(
                    {
                        "ready": True,
                        "download": f"/api/campaigns/{match.group('campaign')}/download",
                        "file_name": exported.name,
                    }
                )
                return
            match = OPEN_ROUTE.fullmatch(path)
            if match:
                _open_repository(self.server.custodian.repository_path(match.group("campaign")))
                self._json_response({"opened": True})
                return
            self._error_response(HTTPStatus.NOT_FOUND, "Page not found", "not_found")
        except Exception as exc:
            self._handle_exception(exc)

    def log_message(self, format: str, *args: object) -> None:
        logging.info("custodian_http " + format, *args)

    def _html_response(self) -> None:
        nonce = secrets.token_urlsafe(18)
        content = _render_application(self.server.csrf_token, nonce).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header(
            "Content-Security-Policy",
            (
                "default-src 'self'; img-src 'self' data:; connect-src 'self'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            ),
        )
        self.send_header(
            "Set-Cookie",
            f"ras_session={self.server.session_secret}; HttpOnly; SameSite=Strict; Path=/",
        )
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def _json_response(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = (
            json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def _bytes_response(
        self,
        media_type: str,
        content: bytes,
        *,
        disposition: str | None = None,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(content)

    def _error_response(self, status: HTTPStatus, message: str, code: str) -> None:
        self._json_response({"error": code, "message": message, "technical_details": code}, status)

    def _handle_exception(self, exc: Exception) -> None:
        if isinstance(exc, (CustodianError, ValidationError, ValueError)):
            message = str(exc).splitlines()[0][:1024]
            self._error_response(HTTPStatus.BAD_REQUEST, message, _error_code(exc))
            return
        error_id = secrets.token_hex(8)
        logging.exception("custodian_internal_error id=%s", error_id)
        self._error_response(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "The application stopped this action safely. Your campaign state was not changed.",
            f"internal_error_{error_id}",
        )

    def _read_json_body(self) -> dict[str, object]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise ValueError("This action requires application data from the local interface.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Request size is invalid.") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request is empty or too large.")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Request data is invalid.") from exc
        if not isinstance(value, dict):
            raise ValueError("Request data is invalid.")
        return value

    def _validate_host(self) -> None:
        host = self.headers.get("Host", "")
        allowed = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        if host not in allowed:
            raise ValueError("Local application host is invalid.")

    def _require_session(self) -> None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookie.get("ras_session")
        if session is None or not secrets.compare_digest(session.value, self.server.session_secret):
            raise ValueError("Local application session expired. Reload the page.")

    def _require_csrf(self) -> None:
        token = self.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(token, self.server.csrf_token):
            raise ValueError("Local application request could not be verified.")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {
            f"http://127.0.0.1:{self.server.server_port}",
            f"http://localhost:{self.server.server_port}",
        }:
            raise ValueError("Local application request origin is invalid.")

    def _send_security_headers(self) -> None:
        for name, value in self._security_headers.items():
            self.send_header(name, value)


def serve(
    data_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    readiness_instance: str | None = None,
    core_socket: Path = DEFAULT_CORE_SOCKET,
    custodian: CampaignCustodian | None = None,
) -> None:
    if host != "127.0.0.1":
        raise ValueError("Campaign Custodian may bind only to 127.0.0.1")
    active_custodian = custodian or CampaignCustodian(data_root, core_socket=core_socket)
    session_secret = secrets.token_urlsafe(32)
    server = CustodianHTTPServer(
        (host, port),
        active_custodian,
        session_secret=session_secret,
        readiness_instance=readiness_instance,
    )
    url = f"http://{host}:{server.server_port}/"
    readiness = active_custodian.custodian_state / "backend-readiness.json"
    atomic_write_json(
        readiness,
        {
            "schema_version": 1,
            "pid": os.getpid(),
            "url": url,
            "started": True,
            "qualified_commit": os.environ.get("RAS_QUALIFIED_COMMIT", "development"),
            "readiness_instance": server.readiness_instance,
        },
        error_factory=CustodianStateError,
        error_message="Backend readiness could not be committed.",
    )
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        readiness.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research Supervisor Campaign Custodian")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path.home() / ".local/share/research-automation-supervisor",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--readiness-instance")
    parser.add_argument("--core-socket", type=Path, default=DEFAULT_CORE_SOCKET)
    args = parser.parse_args(argv)
    log_root = args.data_dir / "custodian-state"
    log_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_root / "technical-details.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        serve(
            args.data_dir,
            host=args.host,
            port=args.port,
            open_browser=args.open_browser,
            readiness_instance=args.readiness_instance,
            core_socket=args.core_socket,
        )
    except OSError:
        return 2
    return 0


def _wizard_submission(value: dict[str, object]) -> WizardSubmissionV1:
    supporting = _supporting_files(value.get("supporting_files", []))
    settings_value = value.get("requested_settings", {})
    if not isinstance(settings_value, dict):
        raise ValueError("Campaign settings are invalid.")
    editable = settings_value.get("editable_areas", ["**"])
    if isinstance(editable, str):
        editable = [item.strip() for item in re.split(r"[,\n]", editable) if item.strip()]
    settings = CampaignProfileSettingsV1.model_validate(
        {
            "profile": settings_value.get("profile", "standard"),
            "worker_model": settings_value.get("worker_model", "gpt-5.6-sol"),
            "worker_reasoning_effort": settings_value.get("worker_reasoning_effort", "high"),
            "auditor_model": settings_value.get("auditor_model", "gpt-5.6-sol"),
            "auditor_reasoning_effort": settings_value.get("auditor_reasoning_effort", "high"),
            "supervisor_model": settings_value.get("supervisor_model", "gpt-5.6-sol"),
            "supervisor_reasoning_effort": settings_value.get(
                "supervisor_reasoning_effort", "high"
            ),
            "max_repair_rounds": settings_value.get("max_repair_rounds", 2),
            "editable_areas": editable,
        }
    )
    return WizardSubmissionV1(
        human_name=_required_string(value, "human_name"),
        repository_kind=_required_string(value, "repository_kind"),  # type: ignore[arg-type]
        repository_locator=_required_string(value, "repository_locator"),
        research_contract=_required_string(value, "research_contract"),
        research_plan=_required_string(value, "research_plan"),
        initial_task=_required_string(value, "initial_task"),
        supporting_files=supporting,
        requested_settings=settings,
    )


def _supporting_files(value: object) -> tuple[FrozenInputFileV1, ...]:
    if not isinstance(value, list):
        raise ValueError("Supporting files are invalid.")
    files: list[FrozenInputFileV1] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Supporting file is invalid.")
        name = _required_string(item, "display_name")
        encoded = _required_string(item, "content_base64")
        media_type = item.get("media_type", "application/octet-stream")
        if not isinstance(media_type, str):
            raise ValueError("Supporting file type is invalid.")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("Supporting file content is invalid.") from exc
        files.append(FrozenInputFileV1.from_bytes(name, content, media_type=media_type))
    return tuple(files)


def _response_uploads(value: object) -> tuple[tuple[str, bytes], ...]:
    if not isinstance(value, list):
        raise ValueError("Response uploads are invalid.")
    uploads: list[tuple[str, bytes]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Response upload is invalid.")
        name = _required_string(item, "display_name")
        encoded = _required_string(item, "content_base64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("Response upload content is invalid.") from exc
        uploads.append((name, content))
    return tuple(uploads)


def _required_string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key.replace('_', ' ').title()} is required.")
    return item


def _pick_repository_folder() -> str:
    if _is_wsl() and _which("powershell.exe"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$dialog=New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$dialog.Description='Choose a Git repository'; "
            "if($dialog.ShowDialog() -eq 'OK'){[Console]::Write($dialog.SelectedPath)}"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        selected = completed.stdout.strip()
        if completed.returncode == 0 and selected:
            converted = subprocess.run(
                ["wslpath", "-u", selected],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if converted.returncode == 0 and converted.stdout.strip():
                return converted.stdout.strip()
    if _which("zenity"):
        completed = subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Choose a Git repository"],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    raise ValueError("Folder picker is unavailable. Drag the repository folder into the field.")


def _open_repository(path: Path) -> None:
    resolved = path.resolve(strict=True)
    if _is_wsl() and _which("explorer.exe"):
        converted = subprocess.run(
            ["wslpath", "-w", str(resolved)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if converted.returncode == 0:
            subprocess.Popen(
                ["explorer.exe", converted.stdout.strip()],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return
    if _which("xdg-open"):
        subprocess.Popen(
            ["xdg-open", str(resolved)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return
    raise ValueError("Repository folder could not be opened automatically.")


def _read_download(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("Campaign export is unavailable.")
    return path.read_bytes()


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().casefold()
    except OSError:
        return False


def _which(name: str) -> bool:
    import shutil

    return shutil.which(name) is not None


def _error_code(exc: Exception) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return name[:80]


def _render_application(csrf: str, nonce: str) -> str:
    return _APPLICATION_HTML.replace("__CSRF__", csrf).replace("__NONCE__", nonce)


_APPLICATION_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Research Supervisor</title>
  <style nonce="__NONCE__">
    :root{--ink:#17211b;--muted:#657168;--paper:#f7f5ef;--card:#fffdf7;--line:#d9ddd3;--green:#1e6650;--mint:#dcebe3;--amber:#a35d18;--amber-bg:#f8e8cc;--blue:#315a74;--red:#8c3c32;--shadow:0 16px 44px rgba(31,44,34,.09)}
    *{box-sizing:border-box} body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif} button,input,textarea,select{font:inherit} button{cursor:pointer}
    .shell{min-height:100vh;display:grid;grid-template-columns:250px 1fr}.sidebar{background:#173f34;color:#edf5ef;padding:28px 22px;position:sticky;top:0;height:100vh}.brand{display:flex;gap:12px;align-items:center;margin-bottom:35px}.mark{width:38px;height:38px;border-radius:12px;background:#d8eadf;color:#173f34;display:grid;place-items:center;font-weight:900}.brand small{display:block;color:#b8d3c6}.nav button{display:block;width:100%;text-align:left;border:0;background:transparent;color:#d9e8df;padding:12px;border-radius:10px;margin:4px 0}.nav button:hover,.nav button.active{background:#245246;color:white}.side-note{position:absolute;bottom:24px;left:22px;right:22px;color:#aac7ba;font-size:12px}.main{padding:42px clamp(24px,5vw,72px);max-width:1250px;width:100%}
    .top{display:flex;justify-content:space-between;gap:25px;align-items:flex-start;margin-bottom:34px}.eyebrow{text-transform:uppercase;letter-spacing:.16em;font-size:11px;font-weight:800;color:var(--green)}h1{font:600 clamp(32px,5vw,54px)/1.05 Georgia,serif;margin:7px 0 10px}.lede{color:var(--muted);max-width:700px;font-size:17px}.primary,.secondary,.ghost{border-radius:11px;padding:11px 17px;font-weight:750}.primary{background:var(--green);color:white;border:1px solid var(--green)}.primary:hover{background:#174f3e}.secondary{background:white;color:var(--green);border:1px solid #9eb8aa}.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}.danger{color:var(--red)}
    .stats{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:12px;margin:22px 0 34px}.stat{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}.stat b{font:600 28px Georgia,serif;display:block}.stat span{color:var(--muted);font-size:13px}.section-head{display:flex;justify-content:space-between;align-items:center;margin:28px 0 13px}.section-head h2{font:600 24px Georgia,serif;margin:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:15px}.campaign-card,.panel,.action-card,.result-card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:21px;box-shadow:0 4px 18px rgba(35,48,39,.04)}.campaign-card{cursor:pointer;color:var(--ink);font:inherit;text-align:left;width:100%}.campaign-card:hover,.campaign-card:focus-visible{border-color:#9eb8aa;box-shadow:var(--shadow)}.pill{display:inline-flex;align-items:center;gap:7px;border-radius:99px;padding:5px 10px;background:var(--mint);color:var(--green);font-size:12px;font-weight:800}.pill.needs_input,.pill.blocked{background:var(--amber-bg);color:var(--amber)}.pill.completed{background:#e0e8f1;color:var(--blue)}.dot{width:7px;height:7px;border-radius:50%;background:currentColor}.campaign-card h3{font:600 20px Georgia,serif;margin:14px 0 5px}.repo{color:var(--muted);font-size:13px;margin-bottom:17px;display:block}.facts{border-top:1px solid var(--line);padding-top:14px;display:block}.fact{margin:6px 0;display:block}.fact span{color:var(--muted)}
    .hidden{display:none!important}.overlay{position:fixed;inset:0;background:rgba(17,29,22,.58);display:grid;place-items:center;padding:22px;z-index:20}.modal{background:var(--paper);width:min(850px,100%);max-height:92vh;overflow:auto;border-radius:22px;box-shadow:0 30px 90px rgba(0,0,0,.25)}.modal-head{padding:24px 28px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;background:var(--paper);z-index:2}.modal-head h2{font:600 27px Georgia,serif;margin:0}.close{border:0;background:transparent;font-size:26px}.modal-body{padding:27px}.steps{display:flex;gap:8px;margin-bottom:26px}.step{flex:1;height:5px;border-radius:4px;background:#d9ded8}.step.on{background:var(--green)}label{font-weight:750;display:block;margin:16px 0 7px}.hint{color:var(--muted);font-size:13px;margin-top:5px}.choice-row{display:flex;gap:10px;flex-wrap:wrap}.choice{border:1px solid var(--line);border-radius:13px;padding:13px;background:white}.choice.active{border-color:var(--green);background:var(--mint)}input[type=text],input[type=url],textarea,select{width:100%;border:1px solid #bdc7be;background:white;border-radius:11px;padding:12px;color:var(--ink)}textarea{min-height:150px;resize:vertical}.drop{border:1.5px dashed #9faf9f;border-radius:14px;padding:18px;background:#fbfcf8}.drop.drag{background:var(--mint);border-color:var(--green)}.wizard-actions{display:flex;justify-content:space-between;margin-top:25px}.preview-list{display:grid;gap:10px}.preview-row{display:flex;justify-content:space-between;gap:20px;padding:13px;border-bottom:1px solid var(--line)}.preview-row span{color:var(--muted)}details{margin-top:16px}summary{cursor:pointer;color:var(--muted);font-weight:700}
    .detail-head{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:24px}.back{border:0;background:transparent;color:var(--green);font-weight:800;padding:0;margin-bottom:13px}.status-hero{background:#173f34;color:white;border-radius:20px;padding:28px;margin-bottom:18px}.status-hero .eyebrow{color:#aed2c0}.status-hero h2{font:600 34px Georgia,serif;margin:6px 0}.status-hero p{color:#c9ddd3;margin:7px 0}.status-hero strong{color:white}.action-card{border-left:5px solid var(--amber);background:#fffaf0}.action-card h3{font:600 25px Georgia,serif;margin:4px 0 13px}.why{background:#f4eee0;border-radius:11px;padding:14px;margin:13px 0}.options{display:grid;gap:9px;margin:14px 0}.option{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);background:white;border-radius:12px;padding:12px}.option small{display:block;color:var(--muted)}.links{display:flex;gap:9px;flex-wrap:wrap;margin:14px 0}.linkbtn{display:inline-block;text-decoration:none;border:1px solid #aac0b3;color:var(--green);background:white;padding:9px 12px;border-radius:9px;font-weight:750}.result-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.metric{padding:15px;background:#f0f3ed;border-radius:11px}.metric b{display:block;font-size:24px}.toast{position:fixed;right:25px;bottom:25px;background:#173f34;color:white;padding:15px 18px;border-radius:12px;box-shadow:var(--shadow);max-width:360px;z-index:30}.error{background:#f8e4df;color:#78372f;border:1px solid #e3b2aa;padding:13px;border-radius:10px;margin:12px 0}.empty{color:var(--muted);padding:25px;border:1px dashed var(--line);border-radius:14px;text-align:center}
    @media(max-width:780px){.shell{display:block}.sidebar{height:auto;position:static;padding:15px}.brand{margin:0}.nav,.side-note{display:none}.main{padding:25px 17px}.top{display:block}.top .primary{margin-top:15px}.stats{grid-template-columns:repeat(2,1fr)}.result-grid{grid-template-columns:1fr}.modal-body{padding:20px}.preview-row{display:block}.preview-row b{display:block;margin-top:4px}}
  </style>
</head>
<body>
<div class="shell">
  <aside class="sidebar"><div class="brand"><div class="mark">RS</div><div><strong>Research Supervisor</strong><small>Campaign Custodian</small></div></div><nav class="nav"><button class="active" data-home>Campaigns</button><button data-new>New Campaign</button><button id="notifyBtn">Enable notifications</button></nav><div class="side-note">Local-only interface<br>Scientific authority stays in the qualified core.</div></aside>
  <main class="main"><div id="home"><div class="top"><div><div class="eyebrow">Research Automation Supervisor</div><h1>Your research campaigns,<br>without the command line.</h1><div class="lede">Start, supervise, answer human requests, and inspect verified results from one local workspace.</div></div><button class="primary" data-new>+ New Campaign</button></div><div id="stats" class="stats"></div><div class="section-head"><h2>Campaigns</h2><button class="ghost" id="refreshBtn">Refresh</button></div><div id="campaigns" class="grid"><div class="empty">Loading campaigns…</div></div></div><div id="detail" class="hidden"></div></main>
</div>
<div id="wizard" class="overlay hidden" role="dialog" aria-modal="true" aria-labelledby="wizardTitle"><div class="modal"><div class="modal-head"><h2 id="wizardTitle">New Campaign</h2><button class="close" data-close aria-label="Close">×</button></div><div class="modal-body"><div class="steps"><i class="step on"></i><i class="step"></i><i class="step"></i><i class="step"></i><i class="step"></i></div><div id="wizardError" aria-live="assertive"></div>
  <section class="wizard-page" data-page="0"><div class="eyebrow">Step 1 of 5</div><h2>Choose the research repository</h2><p class="hint">A separate campaign worktree will be prepared automatically. Your current branch is not changed.</p><div class="choice-row"><button class="choice active" aria-pressed="true" data-kind="existing_folder">Choose existing folder</button><button class="choice" aria-pressed="false" data-kind="git_url">Paste Git URL</button></div><label for="repoLocator">Repository</label><div class="choice-row"><input id="repoLocator" type="text" placeholder="Choose a repository folder" style="flex:1"><button class="secondary" id="pickFolder">Choose folder…</button></div></section>
  <section class="wizard-page hidden" data-page="1"><div class="eyebrow">Step 2 of 5</div><h2>Add the Research Contract</h2><p class="hint">Paste the contract or drop a text file. It will be frozen exactly when you press Start.</p><div class="drop" data-drop="contract"><input type="file" id="contractFile" accept=".md,.txt,.yaml,.yml,.json"><textarea id="contractText" placeholder="Paste the Research Contract here…"></textarea></div></section>
  <section class="wizard-page hidden" data-page="2"><div class="eyebrow">Step 3 of 5</div><h2>Add the Research Plan</h2><p class="hint">The plan is visible to the qualified campaign but cannot change after Start.</p><div class="drop" data-drop="plan"><input type="file" id="planFile" accept=".md,.txt,.yaml,.yml,.json"><textarea id="planText" placeholder="Paste the Research Plan here…"></textarea></div></section>
  <section class="wizard-page hidden" data-page="3"><div class="eyebrow">Step 4 of 5</div><h2>Describe the initial task</h2><label for="campaignName">Campaign name</label><input id="campaignName" type="text" placeholder="Example: Boundary-condition derivation"><label for="taskText">What should the Worker do first?</label><textarea id="taskText" placeholder="Describe the initial research task in ordinary language…"></textarea><label for="supportFiles">Supporting files (optional)</label><input id="supportFiles" type="file" multiple><details><summary>Advanced campaign settings</summary><label for="profile">Acceptance profile</label><select id="profile"><option value="standard">Automatic qualified checks</option><option value="python_pytest">Python — pytest</option><option value="python_unittest">Python — unittest</option></select><label for="editable">Editable areas</label><input id="editable" type="text" value="**"><p class="hint">Leave ** to allow changes anywhere in the selected repository. You can enter comma-separated folders such as src/**, tests/**.</p><label for="repairLimit">Maximum repair rounds</label><select id="repairLimit"><option>0</option><option>1</option><option selected>2</option><option>3</option></select></details></section>
  <section class="wizard-page hidden" data-page="4"><div class="eyebrow">Step 5 of 5</div><h2>Review before Start</h2><div id="previewPanel"><div class="empty">Choose Preview to verify the repository and environment.</div></div></section>
  <div class="wizard-actions"><button class="ghost" id="wizardBack">Back</button><button class="primary" id="wizardNext">Continue</button></div>
</div></div></div>
<div id="toast" class="toast hidden" role="status" aria-live="polite"></div>
<script nonce="__NONCE__">
const CSRF='__CSRF__';let wizardPage=0,repoKind='existing_folder',preview=null,draftRevision=0,currentCampaign=null,currentRequest=null,lastStatuses={},priorFocus=null;
const q=s=>document.querySelector(s),qa=s=>[...document.querySelectorAll(s)];
async function api(path,opts={}){const options={credentials:'same-origin',...opts,headers:{...(opts.headers||{})}};if(options.method&&options.method!=='GET'){options.headers['Content-Type']='application/json';options.headers['X-CSRF-Token']=CSRF;}const r=await fetch(path,options);const type=r.headers.get('content-type')||'';const data=type.includes('json')?await r.json():await r.text();if(!r.ok)throw new Error(data.message||'This action could not be completed.');return data;}
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(message){const t=q('#toast');t.textContent=message;t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),5000)}
function statusLabel(s){return {preparing:'Preparing',running:'Running',needs_input:'Needs input',blocked:'Blocked',completed:'Completed'}[s]||s}
async function loadCampaigns(){try{const data=await api('/api/campaigns');renderCampaigns(data.campaigns);for(const c of data.campaigns){if((lastStatuses[c.campaign_public_id]&&lastStatuses[c.campaign_public_id]!==c.status)||(!lastStatuses[c.campaign_public_id]&&['needs_input','blocked','completed'].includes(c.status)))notifyCampaign(c);lastStatuses[c.campaign_public_id]=c.status;}if(currentCampaign){const found=data.campaigns.find(c=>c.campaign_public_id===currentCampaign);if(found)renderDetail(found);}}catch(e){q('#campaigns').innerHTML=`<div class="error" role="alert">${esc(e.message)}</div>`}}
function renderCampaigns(items){const counts={running:0,needs_input:0,blocked:0,completed:0};items.forEach(i=>counts[i.status]=(counts[i.status]||0)+1);q('#stats').innerHTML=[['Running',counts.running+(counts.preparing||0)],['Needs Input',counts.needs_input],['Blocked',counts.blocked],['Completed',counts.completed]].map(([l,n])=>`<div class="stat"><b>${n}</b><span>${l}</span></div>`).join('');q('#campaigns').innerHTML=items.length?items.map(c=>`<button type="button" class="campaign-card" data-campaign="${esc(c.campaign_public_id)}"><span class="pill ${esc(c.status)}"><i class="dot"></i>${esc(statusLabel(c.status))}</span><h3>${esc(c.human_name)}</h3><span class="repo">${esc(c.repository)}</span><span class="facts"><span class="fact"><span>Stage:</span> ${esc(c.stage)}</span><span class="fact"><span>Last activity:</span> ${esc(c.last_activity)}</span>${c.human_input_needed?'<span class="fact"><strong>Human input is needed</strong></span>':''}</span></button>`).join(''):'<div class="empty">No campaigns yet. Start with New Campaign.</div>';qa('[data-campaign]').forEach(el=>el.onclick=()=>showCampaign(el.dataset.campaign));}
async function showCampaign(id){currentCampaign=id;q('#home').classList.add('hidden');q('#detail').classList.remove('hidden');try{const c=await api(`/api/campaigns/${id}`);renderDetail(c)}catch(e){q('#detail').innerHTML=`<button class="back" data-back>← All campaigns</button><div class="error">${esc(e.message)}</div>`;wireBack()}}
function wireBack(){const b=q('[data-back]');if(b)b.onclick=()=>{currentCampaign=null;q('#detail').classList.add('hidden');q('#home').classList.remove('hidden');loadCampaigns()}}
async function renderDetail(c){if(currentCampaign!==c.campaign_public_id)return;let extra='';if(c.status==='needs_input'){extra='<div id="requestCard" class="action-card" aria-live="polite"><div class="empty">Loading the human request…</div></div>';setTimeout(()=>loadRequest(c),0)}else if(c.status==='completed'){const r=c.result;extra=`<div class="result-card"><div class="eyebrow">Verified result</div><h3>${esc(r.outcome)}</h3><p>${esc(r.executive_summary)}</p><div class="result-grid"><div class="metric"><b>${r.worker_run_count}</b>Worker runs</div><div class="metric"><b>${r.auditor_run_count}</b>Auditor runs</div><div class="metric"><b>${r.repair_count}</b>Repairs</div><div class="metric"><b>${r.human_decision_count}</b>Human decisions</div></div><div class="links">${c.result_links.map(l=>`<a class="linkbtn" target="_blank" rel="noopener" href="/api/campaigns/${esc(c.campaign_public_id)}/artifacts/${esc(l.token)}">${esc(l.label)}</a>`).join('')}<button class="secondary" data-open>Open Repository</button><button class="primary" data-export>Export Campaign Bundle</button></div>${r.final_commit?`<p class="hint">Final commit: ${esc(r.final_commit)}</p>`:''}</div>`}else if(c.status==='blocked'){const auth=c.technical_code==='codex_authentication_required'?'<button class="secondary" data-signin>Sign in</button>':'';extra=`<div class="action-card"><h3>${esc(c.action_title||'Campaign paused safely')}</h3><div class="why">${esc(c.action_message||'The qualified core stopped before another action.')}</div><p>Your scientific campaign state is safe. No action will be repeated without qualified recovery.</p>${auth}<button class="primary" data-continue>Continue</button><details><summary>Technical details</summary><code>${esc(c.technical_code||'qualified_pause')}</code></details></div>`}else if(c.status==='running'){extra='<p><button class="secondary" data-continue>Check or resume safely</button></p>'}const campaignStateText=c.status==='running'?'Campaign is running':`Campaign ${statusLabel(c.status).toLowerCase()}`;
q('#detail').innerHTML=`<button class="back" data-back>← All campaigns</button><div class="detail-head"><div><div class="eyebrow">${esc(c.repository)}</div><h1>${esc(c.human_name)}</h1></div><span class="pill ${esc(c.status)}"><i class="dot"></i>${esc(statusLabel(c.status))}</span></div><div class="status-hero"><div class="eyebrow">${esc(campaignStateText)}</div><h2>Stage: ${esc(c.stage)}</h2><p><strong>Last activity:</strong> ${esc(c.last_activity)}</p></div>${extra}`;wireBack();const cont=q('[data-continue]');if(cont)cont.onclick=()=>continueCampaign(c.campaign_public_id);const signin=q('[data-signin]');if(signin)signin.onclick=signIn;const exp=q('[data-export]');if(exp)exp.onclick=()=>exportCampaign(c.campaign_public_id);const open=q('[data-open]');if(open)open.onclick=()=>openRepo(c.campaign_public_id);}
async function loadRequest(c){try{const r=await api(`/api/campaigns/${c.campaign_public_id}/request`);currentRequest=r;const card=q('#requestCard');if(!card)return;const needsText=r.response_type==='free_text',needsFile=r.response_type==='file_upload';card.innerHTML=`<div class="eyebrow">Action needed</div><h3>Research Supervisor needs input</h3><div class="why"><strong>Why:</strong><br>${esc(r.reason)}</div><p><strong>${esc(r.question)}</strong></p><div class="options">${r.allowed_options.map(o=>`<label class="option"><input type="radio" name="responseOption" value="${esc(o.option_id)}"><span><strong>${esc(o.label)}</strong>${o.consequence?`<small>${esc(o.consequence)}</small>`:''}</span></label>`).join('')}</div>${r.evidence_links.length?`<p><strong>Relevant material:</strong></p><div class="links">${r.evidence_links.map(l=>`<a class="linkbtn" target="_blank" rel="noopener" href="/api/campaigns/${esc(c.campaign_public_id)}/artifacts/${esc(l.token)}">${esc(l.label)}</a>`).join('')}</div>`:''}<label for="responseNote">${needsText?'Response':'Optional note'}</label><textarea id="responseNote" placeholder="Add context for the qualified workflow…"></textarea><label for="responseFiles">${needsFile?'Required file':'Attach files (optional)'}</label><input id="responseFiles" type="file" multiple><p><button class="primary" id="submitResponse">Submit response</button></p><p class="hint">The Custodian will not choose an answer. Your exact response is validated by the qualified core.</p>`;q('#submitResponse').onclick=()=>submitResponse(c.campaign_public_id);}catch(e){const card=q('#requestCard');if(card)card.innerHTML=`<div class="error">${esc(e.message)}</div>`}}
async function submitResponse(id){const selected=q('input[name=responseOption]:checked'),text=q('#responseNote').value,files=q('#responseFiles').files;if(currentRequest.allowed_options.length&&!selected){toast('Choose one response.');return}if(currentRequest.response_type==='free_text'&&!text.trim()){toast('Enter your response.');return}if(currentRequest.response_type==='file_upload'&&!files.length){toast('Attach the requested file.');return}try{const uploads=await filesPayload(files);await api(`/api/campaigns/${id}/respond`,{method:'POST',body:JSON.stringify({selected_option_id:selected?selected.value:null,response_text:text,uploads})});toast('Response submitted to the qualified workflow.');setTimeout(()=>showCampaign(id),800)}catch(e){toast(e.message)}}
async function continueCampaign(id){try{await api(`/api/campaigns/${id}/continue`,{method:'POST',body:'{}'});toast('Qualified recovery is checking the campaign.');setTimeout(()=>showCampaign(id),1000)}catch(e){toast(e.message)}}
async function exportCampaign(id){try{const data=await api(`/api/campaigns/${id}/export`,{method:'POST',body:'{}'});window.location=data.download}catch(e){toast(e.message)}}
async function openRepo(id){try{await api(`/api/campaigns/${id}/open`,{method:'POST',body:'{}'});toast('Repository opened.')}catch(e){toast(e.message)}}
function notifyCampaign(c){if(Notification.permission!=='granted')return;const title=c.status==='completed'?'Campaign completed':c.status==='needs_input'?'Research Supervisor needs input':c.status==='blocked'?'Campaign paused safely':'Campaign update';new Notification(title,{body:`${c.human_name} — ${c.last_activity}`})}
q('#notifyBtn').onclick=async()=>{if(!('Notification'in window)){toast('Browser notifications are unavailable.');return}const p=await Notification.requestPermission();toast(p==='granted'?'Notifications enabled.':'Notifications were not enabled.');if(p==='granted'){lastStatuses={};loadCampaigns()}};
function openWizard(){priorFocus=document.activeElement;wizardPage=0;preview=null;q('.shell').inert=true;q('#wizard').classList.remove('hidden');showWizardPage();q('#repoLocator').focus()}function closeWizard(){q('#wizard').classList.add('hidden');q('.shell').inert=false;if(priorFocus)priorFocus.focus()}qa('[data-new]').forEach(b=>b.onclick=openWizard);qa('[data-close]').forEach(b=>b.onclick=closeWizard);q('#refreshBtn').onclick=loadCampaigns;document.addEventListener('keydown',e=>{const open=!q('#wizard').classList.contains('hidden');if(e.key==='Escape'&&open)closeWizard();if(e.key==='Tab'&&open){const focusable=qa('#wizard button:not([disabled]),#wizard input:not([disabled]),#wizard textarea:not([disabled]),#wizard select:not([disabled]),#wizard a[href]').filter(el=>el.offsetParent!==null);if(!focusable.length)return;const first=focusable[0],last=focusable[focusable.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}});
qa('[data-kind]').forEach(b=>b.onclick=()=>{repoKind=b.dataset.kind;invalidatePreview();qa('[data-kind]').forEach(x=>{x.classList.toggle('active',x===b);x.setAttribute('aria-pressed',x===b?'true':'false')});q('#repoLocator').placeholder=repoKind==='git_url'?'https://github.com/organization/repository.git':'Choose a repository folder';q('#repoLocator').type=repoKind==='git_url'?'url':'text';q('#pickFolder').classList.toggle('hidden',repoKind==='git_url')});
q('#pickFolder').onclick=async()=>{try{const r=await api('/api/pick-folder',{method:'POST',body:'{}'});q('#repoLocator').value=r.path}catch(e){toast(e.message)}};
function showWizardPage(){qa('.wizard-page').forEach((p,i)=>p.classList.toggle('hidden',i!==wizardPage));qa('.step').forEach((s,i)=>s.classList.toggle('on',i<=wizardPage));q('#wizardBack').style.visibility=wizardPage?'visible':'hidden';q('#wizardNext').textContent=wizardPage===4?(preview?'Start Campaign':'Preview'):'Continue';q('#wizardError').innerHTML=''}
function invalidatePreview(){draftRevision++;preview=null;if(wizardPage===4)q('#previewPanel').innerHTML='<div class="empty">Inputs changed. Choose Preview again.</div>'}qa('#repoLocator,#contractText,#planText,#campaignName,#taskText,#supportFiles,#profile,#editable,#repairLimit').forEach(el=>{el.addEventListener('input',invalidatePreview);el.addEventListener('change',invalidatePreview)});q('#wizardBack').onclick=()=>{if(wizardPage>0){invalidatePreview();wizardPage--;showWizardPage()}};q('#wizardNext').onclick=async()=>{try{if(wizardPage<4){validatePage(wizardPage);wizardPage++;showWizardPage();return}if(!preview){await makePreview()}else{await startCampaign()}}catch(e){q('#wizardError').innerHTML=`<div class="error" role="alert">${esc(e.message)}</div>`}};
function validatePage(page){if(page===0&&!q('#repoLocator').value.trim())throw new Error('Choose or paste a repository.');if(page===1&&!q('#contractText').value.trim())throw new Error('Add the Research Contract.');if(page===2&&!q('#planText').value.trim())throw new Error('Add the Research Plan.');if(page===3&&(!q('#campaignName').value.trim()||!q('#taskText').value.trim()))throw new Error('Add a campaign name and initial task.')}
async function makePreview(){validatePage(3);const revision=draftRevision;q('#wizardNext').disabled=true;q('#wizardBack').disabled=true;q('#wizardNext').textContent='Checking…';const supporting=await filesPayload(q('#supportFiles').files);const body={human_name:q('#campaignName').value,repository_kind:repoKind,repository_locator:q('#repoLocator').value,research_contract:q('#contractText').value,research_plan:q('#planText').value,initial_task:q('#taskText').value,supporting_files:supporting,requested_settings:{profile:q('#profile').value,editable_areas:q('#editable').value,max_repair_rounds:Number(q('#repairLimit').value)}};try{const checked=await api('/api/preview',{method:'POST',body:JSON.stringify(body)});if(revision!==draftRevision)throw new Error('Inputs changed while Preview was running. Choose Preview again.');preview=checked;renderPreview(preview)}finally{q('#wizardNext').disabled=false;q('#wizardBack').disabled=false;q('#wizardNext').textContent=preview?'Start Campaign':'Preview'}}
function renderPreview(p){const env=p.environment.ready?'<span class="pill"><i class="dot"></i>Environment ready</span>':`<span class="pill blocked"><i class="dot"></i>Setup needed</span>${p.environment.issues.map(i=>`<div class="why"><strong>${esc(i.title)}</strong><br>${esc(i.message)}${i.action==='sign_in'?'<br><button class="secondary" id="signInBtn">Sign in</button>':''}</div>`).join('')}`;q('#previewPanel').innerHTML=`${env}<div class="preview-list"><div class="preview-row"><span>Campaign</span><b>${esc(p.human_name)}</b></div><div class="preview-row"><span>Repository</span><b>${esc(p.repository)}</b></div><div class="preview-row"><span>Repository version</span><b>${esc(p.baseline_commit_short)}</b></div><div class="preview-row"><span>Research Contract</span><b>Ready to freeze</b></div><div class="preview-row"><span>Research Plan</span><b>Ready to freeze</b></div><div class="preview-row"><span>Initial Task</span><b>${esc(q('#taskText').value.slice(0,240))}</b></div><div class="preview-row"><span>Supporting files</span><b>${p.supporting_file_count}</b></div><div class="preview-row"><span>Acceptance</span><b>${esc(p.profile_summary)}</b></div><div class="preview-row"><span>Editable areas</span><b>${esc(p.editable_areas_summary)}</b></div></div><p class="hint">After Start, these scientific inputs are immutable. Changes require a new campaign or an explicit qualified human-action path.</p>`;const sign=q('#signInBtn');if(sign)sign.onclick=signIn}
async function signIn(){try{const r=await api('/api/sign-in',{method:'POST',body:'{}'});toast(r.message+' After signing in, choose Preview or Continue again.')}catch(e){toast(e.message)}}
async function startCampaign(){q('#wizardNext').disabled=true;q('#wizardNext').textContent='Starting…';try{const key='start_'+crypto.randomUUID().replaceAll('-','');const c=await api('/api/start',{method:'POST',body:JSON.stringify({preview_id:preview.preview_id,start_key:key})});closeWizard();currentCampaign=c.campaign_public_id;q('#home').classList.add('hidden');q('#detail').classList.remove('hidden');renderDetail(c);toast(c.status==='blocked'?'Campaign inputs are frozen; setup is needed before launch.':'Campaign started through the qualified core.')}finally{q('#wizardNext').disabled=false;q('#wizardNext').textContent='Start Campaign'}}
async function fileText(input,target){const f=input.files[0];if(f)q(target).value=await f.text()}q('#contractFile').onchange=()=>fileText(q('#contractFile'),'#contractText');q('#planFile').onchange=()=>fileText(q('#planFile'),'#planText');qa('.drop').forEach(d=>{d.ondragover=e=>{e.preventDefault();d.classList.add('drag')};d.ondragleave=()=>d.classList.remove('drag');d.ondrop=async e=>{e.preventDefault();d.classList.remove('drag');const f=e.dataTransfer.files[0];if(f)q(d.dataset.drop==='contract'?'#contractText':'#planText').value=await f.text()}});
async function filesPayload(files){const out=[];for(const f of [...files]){const bytes=new Uint8Array(await f.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));out.push({display_name:f.name,media_type:f.type||'application/octet-stream',content_base64:btoa(binary)})}return out}
q('[data-home]').onclick=()=>{currentCampaign=null;q('#detail').classList.add('hidden');q('#home').classList.remove('hidden');loadCampaigns()};loadCampaigns();setInterval(loadCampaigns,5000);
</script>
</body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
