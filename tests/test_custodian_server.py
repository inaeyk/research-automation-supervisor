from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from research_automation_supervisor.custodian import CampaignCustodian
from research_automation_supervisor.custodian_exchange import (
    prepare_operator_exchange,
    publish_human_action_request,
)
from research_automation_supervisor.custodian_server import CustodianHTTPServer
from tests.custodian_helpers import (
    FakeQualifiedRunner,
    create_repository,
    projection,
    ready_environment,
)
from tests.test_custodian import human_request, token_factory


class BrowserClient:
    def __init__(self, server: CustodianHTTPServer) -> None:
        self.server = server
        self.root = f"http://127.0.0.1:{server.server_port}"
        self.cookie = "ras_session=browser-session"

    def get(self, path: str) -> tuple[bytes, dict[str, str]]:
        request = urllib.request.Request(
            self.root + path,
            headers={"Cookie": self.cookie},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read(), dict(response.headers)

    def post(self, path: str, value: object, *, csrf: str | None = None) -> Any:
        request = urllib.request.Request(
            self.root + path,
            data=json.dumps(value).encode("utf-8"),
            method="POST",
            headers={
                "Cookie": self.cookie,
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf or self.server.csrf_token,
                "Origin": self.root,
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())


def start_server(custodian: CampaignCustodian) -> tuple[CustodianHTTPServer, threading.Thread]:
    server = CustodianHTTPServer(
        ("127.0.0.1", 0),
        custodian,
        session_secret="browser-session",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_server(server: CustodianHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_zero_shell_http_acceptance_journey_and_progressive_disclosure(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    runner = FakeQualifiedRunner()
    custodian = CampaignCustodian(
        tmp_path / "data",
        runner=runner,
        environment_inspector=lambda _path: ready_environment(),
        token_factory=token_factory(),  # type: ignore[arg-type]
    )
    server, thread = start_server(custodian)
    browser = BrowserClient(server)
    try:
        html, headers = browser.get("/")
        page = html.decode("utf-8")
        assert "Your research campaigns" in page
        assert "Campaign is running" in page
        assert "Stage:" in page
        assert "Last activity:" in page
        assert "workflow_state=" not in page
        assert "run_token" not in page
        assert "proof_sha256" not in page
        assert "Content-Security-Policy" in headers
        assert 'role="status" aria-live="polite"' in page
        assert 'id="browserNotice"' in page
        assert "What happened?" in page
        assert "What do you need from me?" in page
        assert "What happens after I answer?" in page
        assert "Reports and evidence" in page
        assert "Final commit" in page
        assert "Human Action Inbox" in page
        assert "Repository state" in page
        assert "Repository version: <code>" in page
        assert "Campaign bundle ready" in page
        assert "link.download=data.file_name" in page
        assert 'class="campaign-card"' in page
        assert "invalidatePreview" in page
        assert "currentRequest.response_type==='free_text'" in page
        assert "data-signin" in page
        assert "Check or resume safely" in page

        preview = browser.post(
            "/api/preview",
            {
                "human_name": "Boundary campaign",
                "repository_kind": "existing_folder",
                "repository_locator": str(repository),
                "research_contract": "Frozen contract.\n",
                "research_plan": "Implement and audit M2-C.\n",
                "initial_task": "Implement M2-C.\n",
                "supporting_files": [],
                "requested_settings": {
                    "profile": "standard",
                    "editable_areas": "**",
                    "max_repair_rounds": 2,
                },
            },
        )
        started = browser.post(
            "/api/start",
            {
                "preview_id": preview["preview_id"],
                "start_key": "start_abcdefghijklmnop",
            },
        )
        campaign_id = started["campaign_public_id"]
        assert started["status"] == "preparing"

        runner.current = projection(campaign_id, status="blocked")
        blocked, _ = browser.get(f"/api/campaigns/{campaign_id}")
        assert json.loads(blocked)["technical_code"] == "unsafe_recovery_blocked"
        browser.post(f"/api/campaigns/{campaign_id}/continue", {})
        assert runner.resumed == 1

        record = custodian.get_record(campaign_id, refresh=False)
        issued = human_request(campaign_id, record.launch_intent_sha256)
        exchange = prepare_operator_exchange(custodian.exchange_root, campaign_id)
        publish_human_action_request(exchange, issued)
        runner.current = projection(
            campaign_id,
            status="needs_input",
            request_sha256=issued.request_sha256,
        )
        waiting, _ = browser.get(f"/api/campaigns/{campaign_id}")
        assert json.loads(waiting)["status"] == "needs_input"
        request, _ = browser.get(f"/api/campaigns/{campaign_id}/request")
        assert "boundary-condition convention" in json.loads(request)["reason"]
        browser.post(
            f"/api/campaigns/{campaign_id}/respond",
            {
                "selected_option_id": "continue_existing",
                "response_text": "Keep the existing locked convention.",
                "uploads": [],
            },
        )
        completed, _ = browser.get(f"/api/campaigns/{campaign_id}")
        completed_value = json.loads(completed)
        assert completed_value["status"] == "completed"
        assert completed_value["completion_verified"] is True
        exported = browser.post(f"/api/campaigns/{campaign_id}/export", {})
        archive, archive_headers = browser.get(exported["download"])
        assert archive.startswith(b"PK")
        assert archive_headers["Content-Type"] == "application/zip"

        # A browser/UI restart is just a new session page load; the campaign stays discoverable.
        reloaded, _ = browser.get("/")
        assert b"Research Supervisor" in reloaded
        campaigns, _ = browser.get("/api/campaigns")
        assert json.loads(campaigns)["campaigns"][0]["status"] == "completed"
    finally:
        stop_server(server, thread)


def test_http_rejects_cross_site_post_without_csrf(tmp_path: Path) -> None:
    runner = FakeQualifiedRunner()
    custodian = CampaignCustodian(
        tmp_path / "data",
        runner=runner,
        environment_inspector=lambda _path: ready_environment(),
    )
    server, thread = start_server(custodian)
    browser = BrowserClient(server)
    try:
        with pytest.raises(urllib.error.HTTPError) as captured:
            browser.post("/api/sign-in", {}, csrf="wrong")
        assert captured.value.code == 400
        error = json.loads(captured.value.read())
        assert "verified" in error["message"]
    finally:
        stop_server(server, thread)
