"""Headless-Chrome check of the analyst case review with server-side identity.

Seeds a disposable SQLite case store, starts the case API on 127.0.0.1:18087 in
proxy mode with a per-run token, starts the console's Vite dev server (whose
proxy forwards the browser's headers to that API), and drives the console with
Playwright using the installed Google Chrome (``channel="chrome"``; no browser
download). Browser-set headers stand in for an authenticating reverse proxy.
The payments API is not started; its route is answered with an empty list.

Writes ``bench/case_review_browser.json``. Prints SKIP and exits 0 when Chrome,
Playwright or the frontend's ``node_modules`` is missing.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

from payguard_fraud.case_store import CaseStore

ROOT = Path(__file__).resolve().parents[3]
FRONTEND = ROOT / "frontend"
RESULT = ROOT / "bench" / "case_review_browser.json"
CASE_PORT = 18087  # Fixed by frontend/vite.config.ts.
MAC_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def skip_reason() -> str | None:
    if not (os.environ.get("CHROME_PATH") or MAC_CHROME.is_file() or shutil.which("google-chrome")):
        return "Google Chrome was not found."
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return "Playwright is not installed; install the fraud package's `browser` extra."
    if not (FRONTEND / "node_modules" / ".bin" / "vite").exists():
        return "frontend/node_modules is missing; run `npm ci` in frontend/."
    return None


def port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_http(url: str, process: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process exited early; see {log}")
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not answer within 60 s")


@contextmanager
def service(command: list[str], cwd: Path, env: dict, url: str, log: Path):
    with log.open("w") as handle:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=handle,
                                   stderr=subprocess.STDOUT)
        try:
            wait_http(url, process, log)
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def seed(path: Path) -> str:
    """Three small authorizations then a high-scoring one on the same card."""
    store = CaseStore(path)
    for index in range(4):
        store.observe({
            "timestamp": f"2026-10-11T08:00:0{index}Z",
            "request": {"authId": f"auth_small_{index}", "cardId": "card_demo",
                        "merchantId": "shop_demo", "amountMinor": 100 + index,
                        "deviceId": "new_device", "merchantRisk": 0.7},
            "result": {"status": "DECLINED", "modelScore": 0.95 if index == 3 else 0.01,
                       "modelReasonCodes": ["small_auths_1m"] if index == 3 else []},
        })
    return "case_auth_small_3"


def main() -> int:
    reason = skip_reason()
    if reason:
        print(f"SKIP: {reason}")
        return 0
    if port_open(CASE_PORT):
        raise SystemExit(f"port {CASE_PORT} is in use; stop the packaged stack first")
    from playwright.sync_api import sync_playwright

    token = secrets.token_urlsafe(24)  # Per-run; never written to evidence.
    started = datetime.now(UTC)
    checks: dict[str, bool] = {}
    observed: dict = {}
    with tempfile.TemporaryDirectory(prefix="payguard-review-") as folder:
        folder = Path(folder)
        case_id = seed(folder / "cases.sqlite")
        api_env = {**os.environ, "PAYGUARD_AUTH_MODE": "proxy", "PAYGUARD_PROXY_TOKEN": token,
                   "PAYGUARD_CASE_DB": str(folder / "cases.sqlite"),
                   "PAYGUARD_OLLAMA_URL": "http://127.0.0.1:9/api/generate"}
        api_env.pop("PAYGUARD_CASE_DSN", None)
        ui_port = free_port()
        with service([sys.executable, "-m", "uvicorn", "payguard_fraud.case_api:app",
                      "--host", "127.0.0.1", "--port", str(CASE_PORT)],
                     ROOT / "fraud", api_env, f"http://127.0.0.1:{CASE_PORT}/health",
                     folder / "api.log"), \
             service([str(FRONTEND / "node_modules" / ".bin" / "vite"), "--host", "127.0.0.1",
                      "--port", str(ui_port), "--strictPort"],
                     FRONTEND, dict(os.environ), f"http://127.0.0.1:{ui_port}/",
                     folder / "vite.log"):
            url = f"http://127.0.0.1:{ui_port}/"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
                chrome = browser.version

                def page_as(headers: dict):
                    context = browser.new_context(extra_http_headers=headers)
                    context.route("**/api/payments/**", lambda route: route.fulfill(json=[]))
                    page = context.new_page()
                    page.goto(url)
                    return context, page

                def identity(user: str, role: str) -> dict:
                    return {"X-PayGuard-User": user, "X-PayGuard-Role": role,
                            "X-PayGuard-Proxy-Token": token}

                context, page = page_as({})
                page.get_by_text("request did not come through the trusted proxy").first.wait_for()
                checks["no_proxy_headers_shows_401_and_no_cases"] = (
                    page.get_by_role("button", name=case_id).count() == 0)
                context.close()

                context, page = page_as(identity("ana", "analyst"))
                page.get_by_role("button", name="Case queue").click()
                page.get_by_role("button", name=case_id).first.click()
                page.get_by_role("button", name="Generate cited draft").click()
                page.get_by_text("RECOMMENDED ACTION").wait_for()
                cited = page.evaluate("""() => [...document.querySelectorAll('.claim button')]
                    .map(b => b.innerText.trim())""")
                evidence = page.evaluate("""() => [...document.querySelectorAll(
                    '.evidence-list button span')].map(s => s.innerText.trim())""")
                page.get_by_text("Recording as ana (analyst)").wait_for()
                approve = page.get_by_role("button", name="Approve recommendation")
                checks["analyst_draft_has_citations"] = len(cited) > 0
                checks["every_citation_is_in_case_evidence"] = (
                    bool(cited) and set(cited) <= set(evidence))
                checks["analyst_sees_identity_and_disabled_review"] = approve.is_disabled() and (
                    page.get_by_text("a lead must decide").count() == 1)
                checks["no_analyst_name_input"] = (
                    page.get_by_label("Analyst", exact=True).count() == 0)
                observed["citations"], observed["evidence"] = cited, evidence
                context.close()

                context, page = page_as(identity("lee", "lead"))
                page.get_by_role("button", name="Case queue").click()
                page.get_by_role("button", name=case_id).first.click()
                page.get_by_text("Recording as lee (lead)").wait_for()
                page.get_by_label("Review note").fill("Customer confirmed card testing")
                page.get_by_role("button", name="Approve recommendation").click()
                history = page.get_by_role("list", name="Review history")
                history.wait_for()
                observed["history"] = history.inner_text()
                checks["lead_review_recorded_under_server_identity"] = (
                    "APPROVED by lee (lead)" in observed["history"])
                checks["review_buttons_gone_after_decision"] = (
                    page.get_by_role("button", name="Approve recommendation").count() == 0)
                api = context.request
                audit = api.get(f"{url}api/cases/audit?case_id={case_id}").json()
                actions = [(row["actor"], row["action"]) for row in reversed(audit)]
                observed["audit_actions"] = actions
                checks["audit_names_drafter_and_reviewer"] = (
                    ("ana", "draft_saved") in actions and ("lee", "review_recorded") in actions)
                again = api.post(f"{url}api/cases/cases/{case_id}/review",
                                 data={"decision": "REJECTED", "note": "late"})
                checks["second_review_refused"] = again.status == 400
                context.close()

                context, page = page_as(identity("ana", "analyst"))
                denied = context.request.get(f"{url}api/cases/audit").status
                checks["analyst_cannot_read_audit"] = denied == 403
                context.close()
                browser.close()
        stopped = not port_open(CASE_PORT) and not port_open(ui_port)
    evidence = {
        "generated_at": started.isoformat(timespec="seconds"),
        "what": "Headless Chrome case review through the Vite proxy with proxy-mode identity",
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "chrome": chrome, "playwright": metadata.version("playwright"),
                        "fastapi": metadata.version("fastapi")},
        "passed": sum(checks.values()), "total": len(checks), "servers_stopped": stopped,
        "checks": checks, "observed": observed,
        "limits": ["Synthetic seeded events in a disposable SQLite store.",
                   "Browser-set headers stand in for an authenticating proxy; no identity "
                   "provider was used. Draft text came from the rules fallback (Ollama off).",
                   "The payments API was not running; its route returned an empty list."],
    }
    RESULT.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": evidence["passed"], "total": evidence["total"],
                      "failed": [name for name, ok in checks.items() if not ok],
                      "servers_stopped": stopped}, indent=2))
    return 0 if evidence["passed"] == evidence["total"] and stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
