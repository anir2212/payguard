"""Headless-Chrome case review through the packaged stack's real sign-in.

Run after ``make up`` (``make case-review-stack-browser``). Unlike
``case_review_browser`` (Vite dev proxy, browser-set identity headers), this
drives the console at the gateway, http://127.0.0.1:18119, and signs in on the
Dex login page as the two demo users. The analyst's browser also sends forged
``X-PayGuard-*`` headers claiming to be a lead, which the gateway must drop.

Seeds one synthetic four-event card history into the stack's PostgreSQL case
database with ``CaseStore.observe`` (the worker's own method), so the run does
not depend on a model score. Writes ``bench/case_review_stack_browser.json``
without tokens or cookies. Prints SKIP and exits 0 when Chrome or Playwright
is missing.
"""

from __future__ import annotations

import json
import platform
from datetime import UTC, datetime, timedelta
from importlib import metadata
from uuid import uuid4

from payguard_fraud.case_review_browser import ROOT
from payguard_fraud.case_review_browser import skip_reason as dev_skip_reason
from payguard_fraud.case_store import CaseStore
from payguard_fraud.stack_identity_check import (
    ANALYST,
    BASE,
    CASE_DSN,
    DEFAULT_PROXY_TOKEN,
    LEAD,
    PASSWORD,
)

RESULT = ROOT / "bench" / "case_review_stack_browser.json"


def skip_reason() -> str | None:
    """Chrome and Playwright are needed; the frontend's node_modules are not."""
    reason = dev_skip_reason()
    return None if reason and "node_modules" in reason else reason


def seed(store: CaseStore, nonce: str) -> str:
    start = datetime.now(UTC) - timedelta(seconds=10)
    for index in range(4):
        store.observe({
            "timestamp": (start + timedelta(seconds=index)).isoformat(timespec="microseconds"),
            "request": {"authId": f"auth_browser_{nonce}_{index}", "cardId": f"card_br_{nonce}",
                        "merchantId": f"shop_br_{nonce}", "amountMinor": 100 + index,
                        "deviceId": f"device_br_{nonce}", "merchantRisk": 0.7},
            "result": {"status": "DECLINED", "modelScore": 0.95 if index == 3 else 0.01,
                       "modelReasonCodes": ["small_auths_1m"] if index == 3 else []},
        })
    return f"case_auth_browser_{nonce}_3"


def main() -> int:
    reason = skip_reason()
    if reason:
        print(f"SKIP: {reason}")
        return 0
    from playwright.sync_api import sync_playwright

    nonce = uuid4().hex[:8]
    case_id = seed(CaseStore(dsn=CASE_DSN), nonce)
    started = datetime.now(UTC)
    checks: dict[str, bool] = {}
    observed: dict = {"case_id": case_id}
    forged = {"X-PayGuard-User": "mallory", "X-PayGuard-Role": "lead",
              "X-PayGuard-Proxy-Token": DEFAULT_PROXY_TOKEN}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        chrome = browser.version

        def signed_in(email: str, headers: dict | None = None):
            context = browser.new_context(extra_http_headers=headers or {})
            page = context.new_page()
            page.goto(BASE + "/")
            page.locator('input[name="login"]').wait_for()
            observed.setdefault("login_page_path", page.url.split("?")[0].replace(BASE, ""))
            page.fill('input[name="login"]', email)
            page.fill('input[name="password"]', PASSWORD)
            page.locator('button[type="submit"]').click()
            page.get_by_role("button", name="Case queue").wait_for()
            return context, page

        context = browser.new_context()
        page = context.new_page()
        page.goto(BASE + "/")
        page.locator('input[name="login"]').wait_for()
        checks["signed_out_page_shows_idp_login"] = (
            "/dex/auth" in page.url and page.get_by_role("button", name="Case queue").count() == 0)
        signed_out = context.request.get(f"{BASE}/api/cases/me").status
        checks["signed_out_case_api_401"] = signed_out == 401
        context.close()

        context, page = signed_in(ANALYST, forged)
        checks["analyst_signed_in_through_idp"] = page.url.startswith(BASE + "/") and (
            "/dex/" not in page.url)
        me = context.request.get(f"{BASE}/api/cases/me").json()
        observed["analyst_me_with_forged_headers"] = me
        checks["forged_lead_headers_ignored"] = (me["name"], me["role"]) == (ANALYST, "analyst")
        page.get_by_role("button", name="Case queue").click()
        page.get_by_role("button", name=case_id).first.click()
        page.get_by_role("button", name="Generate cited draft").click()
        page.get_by_text("RECOMMENDED ACTION").wait_for()
        cited = page.evaluate("""() => [...document.querySelectorAll('.claim button')]
            .map(b => b.innerText.trim())""")
        evidence = page.evaluate("""() => [...document.querySelectorAll(
            '.evidence-list button span')].map(s => s.innerText.trim())""")
        page.get_by_text(f"Recording as {ANALYST} (analyst)").wait_for()
        observed["citations"], observed["evidence"] = cited, evidence
        checks["analyst_draft_citations_in_evidence"] = bool(cited) and set(cited) <= set(evidence)
        checks["analyst_review_buttons_disabled"] = (
            page.get_by_role("button", name="Approve recommendation").is_disabled()
            and page.get_by_text("a lead must decide").count() == 1)
        checks["analyst_review_request_403"] = context.request.post(
            f"{BASE}/api/cases/cases/{case_id}/review",
            data={"decision": "APPROVED", "note": "not allowed"}).status == 403
        checks["analyst_audit_request_403"] = (
            context.request.get(f"{BASE}/api/cases/audit").status == 403)
        context.close()

        context, page = signed_in(LEAD)
        page.get_by_role("button", name="Case queue").click()
        page.get_by_role("button", name=case_id).first.click()
        page.get_by_text(f"Recording as {LEAD} (lead)").wait_for()
        page.get_by_label("Review note").fill("Confirmed card testing")
        page.get_by_role("button", name="Approve recommendation").click()
        history = page.get_by_role("list", name="Review history")
        history.wait_for()
        observed["history"] = history.inner_text()
        checks["lead_decision_recorded_under_idp_identity"] = (
            f"APPROVED by {LEAD} (lead)" in observed["history"])
        audit = context.request.get(f"{BASE}/api/cases/audit?case_id={case_id}").json()
        actions = [(row["actor"], row["action"]) for row in reversed(audit)]
        observed["audit_actions"] = actions
        checks["audit_names_drafter_and_decider"] = (
            (ANALYST, "draft_saved") in actions and (LEAD, "review_recorded") in actions)
        page.goto(f"{BASE}/oauth2/sign_out")
        checks["sign_out_ends_session"] = (
            context.request.get(f"{BASE}/api/cases/me").status == 401)
        context.close()
        browser.close()

    evidence = {
        "generated_at": started.isoformat(timespec="seconds"),
        "what": "Headless Chrome case review through the gateway, oauth2-proxy and Dex login",
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "chrome": chrome, "playwright": metadata.version("playwright")},
        "passed": sum(checks.values()), "total": len(checks),
        "checks": checks, "observed": observed,
        "limits": ["Synthetic seeded events in the stack's PostgreSQL case database.",
                   "Demo users with a public local-only password; loopback HTTP, no TLS.",
                   "Draft text came from whichever path the case API took (Ollama or rules)."],
    }
    RESULT.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": evidence["passed"], "total": evidence["total"],
                      "failed": [name for name, ok in checks.items() if not ok]}, indent=2))
    return 0 if evidence["passed"] == evidence["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
