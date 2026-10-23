"""Check sign-in, roles and the shared case store on the running packaged stack.

Run after ``make up`` (or the same Compose files under another project name):

    cd fraud && .venv/bin/python -m payguard_fraud.stack_identity_check --project payguard

Every request goes through the gateway on 127.0.0.1:18119. The demo users sign
in with the identity provider's password grant, and their ID tokens are sent as
bearer tokens, which oauth2-proxy verifies the same way it verifies a browser
session's token. The script seeds its own synthetic events into the case
database with ``CaseStore.observe`` (the worker's own method), restarts
``cases-api`` once, and reads PostgreSQL directly to confirm what was stored.

Writes ``bench/identity_stack_check.json``. No token or password is written.
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from payguard_fraud.case_store import CaseStore

ROOT = Path(__file__).resolve().parents[3]
RESULT = ROOT / "bench" / "identity_stack_check.json"
BASE = "http://127.0.0.1:18119"
# LOCAL DEMO ONLY values from compose.yaml and deploy/dex.yaml.
CLIENT = ("payguard-console", "local-demo-only-client-secret")
PASSWORD = "local-demo-only"
LEAD, ANALYST = "lead@payguard.local", "analyst@payguard.local"
DEFAULT_PROXY_TOKEN = "local-demo-only-proxy-token"
CASE_DSN = "postgresql://payguard_cases:local-demo-only@127.0.0.1:15439/payguard_cases"
RACE_CASES = 5


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = build_opener(_NoRedirect)


def call(method: str, path: str, token: str | None = None, body: dict | None = None,
         headers: dict | None = None) -> tuple[int, dict | str, dict]:
    """One request through the gateway; returns status, parsed body and headers."""
    sent = dict(headers or {})
    if token:
        sent["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        sent["Content-Type"] = "application/json"
    request = Request(BASE + path, data, sent, method=method)
    try:
        with OPENER.open(request, timeout=60) as response:
            status, raw, reply = response.status, response.read(), dict(response.headers)
    except HTTPError as error:
        status, raw, reply = error.code, error.read(), dict(error.headers)
    try:
        parsed: dict | str = json.loads(raw)
    except ValueError:
        parsed = raw.decode(errors="replace")[:200]
    return status, parsed, reply


def sign_in(email: str, password: str = PASSWORD) -> tuple[int, str | None]:
    """Dex password grant through the gateway; returns status and ID token."""
    form = urlencode({"grant_type": "password", "scope": "openid email profile",
                      "username": email, "password": password}).encode()
    basic = base64.b64encode(f"{CLIENT[0]}:{CLIENT[1]}".encode()).decode()
    request = Request(f"{BASE}/dex/token", form, {
        "Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"})
    try:
        with OPENER.open(request, timeout=30) as response:
            return response.status, json.load(response)["id_token"]
    except HTTPError as error:
        return error.code, None


def port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def seed(store: CaseStore, nonce: str, count: int) -> list[str]:
    """Synthetic high-scoring events, opened as cases exactly as the worker does."""
    cases = []
    for index in range(count):
        case_id = store.observe({
            "timestamp": datetime.now(UTC).isoformat(timespec="microseconds"),
            "request": {"authId": f"auth_idcheck_{nonce}_{index}", "cardId": f"card_idc_{nonce}",
                        "merchantId": "merchant_idcheck", "amountMinor": 100 + index,
                        "deviceId": f"device_idc_{nonce}", "merchantRisk": 0.7},
            "result": {"status": "DECLINED", "modelScore": 0.95,
                       "modelReasonCodes": ["small_auths_1m"]}})
        cases.append(case_id)
    return cases


def wait_healthy(token: str, deadline_s: float = 90) -> float:
    started = time.monotonic()
    while time.monotonic() - started < deadline_s:
        try:
            if call("GET", "/api/cases/me", token)[0] == 200:
                return round(time.monotonic() - started, 1)
        except (URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.5)
    raise RuntimeError("case API did not come back through the gateway")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default="payguard", help="Compose project name")
    args = parser.parse_args()
    import psycopg

    checks: dict[str, bool] = {}
    observed: dict = {}
    nonce = uuid4().hex[:8]
    started = datetime.now(UTC)

    # Signed out.
    status, body, _ = call("GET", "/api/cases/me")
    checks["signed_out_case_api_401"] = status == 401
    status, _, headers = call("GET", "/")
    location = headers.get("Location", "")
    checks["signed_out_page_redirects_to_sign_in"] = (
        status == 302 and location.startswith("/oauth2/start"))
    status, _, headers = call("GET", "/oauth2/start?rd=/")
    checks["sign_in_redirects_to_identity_provider"] = (
        status == 302 and headers.get("Location", "").startswith(f"{BASE}/dex/auth"))
    checks["signed_out_payments_api_401"] = (
        call("GET", "/api/payments/v1/authorizations/recent")[0] == 401)
    forged = {"X-PayGuard-User": "mallory", "X-PayGuard-Role": "lead",
              "X-PayGuard-Proxy-Token": DEFAULT_PROXY_TOKEN}
    checks["forged_headers_without_session_401"] = (
        call("GET", "/api/cases/me", headers=forged)[0] == 401)
    checks["invalid_bearer_token_401"] = call("GET", "/api/cases/me", "not-a-jwt")[0] == 401
    checks["wrong_password_refused_by_idp"] = sign_in(ANALYST, "wrong-password")[0] == 401
    checks["case_api_not_published_on_host"] = not port_open(18087)

    # Signed in.
    analyst_status, analyst = sign_in(ANALYST)
    lead_status, lead = sign_in(LEAD)
    if not (analyst and lead):
        raise SystemExit(f"demo sign-in failed: analyst {analyst_status}, lead {lead_status}")
    status, me, _ = call("GET", "/api/cases/me", analyst)
    observed["analyst_me"] = me
    checks["analyst_identity_from_idp"] = status == 200 and (
        me["name"], me["role"], me["mode"]) == (ANALYST, "analyst", "proxy")
    status, me, _ = call("GET", "/api/cases/me", analyst, headers=forged)
    observed["analyst_me_with_forged_headers"] = me
    checks["client_role_header_ignored"] = status == 200 and (
        me["name"], me["role"]) == (ANALYST, "analyst")
    status, me, _ = call("GET", "/api/cases/me", lead)
    checks["lead_identity_from_idp"] = status == 200 and (me["name"], me["role"]) == (LEAD, "lead")
    checks["signed_in_payments_api_200"] = (
        call("GET", "/api/payments/v1/authorizations/recent", analyst)[0] == 200)

    store = CaseStore(dsn=CASE_DSN)
    case_id, *race_cases = seed(store, nonce, 1 + RACE_CASES)
    observed["case_id"], observed["race_cases"] = case_id, race_cases

    status, cases, _ = call("GET", "/api/cases/cases", analyst)
    detail_status, detail, _ = call("GET", f"/api/cases/cases/{case_id}", analyst)
    checks["analyst_can_list_and_read"] = (
        status == 200 and case_id in [row["case_id"] for row in cases]
        and detail_status == 200 and detail["status"] == "OPEN")
    status, draft, _ = call("POST", f"/api/cases/cases/{case_id}/draft", analyst)
    observed["draft_source"] = draft.get("draftSource") if isinstance(draft, dict) else draft
    checks["analyst_can_draft"] = status == 200
    review = {"decision": "APPROVED", "note": "identity check"}
    status, body, _ = call("POST", f"/api/cases/cases/{case_id}/review", analyst, review)
    checks["analyst_cannot_decide"] = status == 403
    status, body, _ = call("POST", f"/api/cases/cases/{case_id}/review", analyst, review, forged)
    checks["analyst_with_forged_lead_header_cannot_decide"] = status == 403
    checks["analyst_cannot_read_audit"] = call("GET", "/api/cases/audit", analyst)[0] == 403
    status, body, _ = call("POST", f"/api/cases/cases/{case_id}/review", lead, review)
    checks["lead_can_decide"] = status == 200 and body.get("analyst") == LEAD
    checks["second_decision_refused"] = call(
        "POST", f"/api/cases/cases/{case_id}/review", lead,
        {"decision": "REJECTED", "note": "late"})[0] == 400

    def stored(case: str) -> dict:
        with psycopg.connect(CASE_DSN) as db:
            row = db.execute("SELECT status, analyst, drafted_by FROM cases WHERE case_id=%s",
                             (case,)).fetchone()
            reviews = db.execute("""SELECT decision, analyst, role FROM case_reviews
                WHERE case_id=%s ORDER BY review_id""", (case,)).fetchall()
            audit = db.execute("""SELECT actor, role, action FROM case_audit
                WHERE case_id=%s ORDER BY event_id""", (case,)).fetchall()
        return {"case": list(row), "reviews": [list(r) for r in reviews],
                "audit": [list(r) for r in audit]}

    before = stored(case_id)
    observed["postgres_before_restart"] = before
    checks["decision_and_audit_rows_in_postgres"] = (
        before["case"] == ["APPROVED", LEAD, ANALYST]
        and before["reviews"] == [["APPROVED", LEAD, "lead"]]
        and [ANALYST, "analyst", "draft_saved"] in before["audit"]
        and [LEAD, "lead", "review_recorded"] in before["audit"]
        and before["audit"][0] == ["system:case-stream", "system", "case_opened"])

    subprocess.run(["docker", "compose", "-p", args.project, "--profile", "stream",
                    "--profile", "packaged", "restart", "cases-api"],
                   cwd=ROOT, check=True, capture_output=True)
    observed["seconds_until_case_api_answered_after_restart"] = wait_healthy(lead)
    status, detail, _ = call("GET", f"/api/cases/cases/{case_id}", lead)
    after = stored(case_id)
    observed["postgres_after_restart"] = after
    checks["decision_survives_cases_api_restart"] = (
        status == 200 and detail["status"] == "APPROVED" and detail["analyst"] == LEAD
        and [(r["decision"], r["analyst"]) for r in detail["reviews"]] == [("APPROVED", LEAD)]
        and after["audit"][:len(before["audit"])] == before["audit"])

    # Two simultaneous lead decisions per case, through the gateway.
    for case in race_cases:
        assert call("POST", f"/api/cases/cases/{case}/draft", analyst)[0] == 200
    race: dict[str, list[int]] = {}
    for case in race_cases:
        barrier, statuses = threading.Barrier(2), []

        def decide(decision: str, case: str = case, barrier=barrier, statuses=statuses) -> None:
            barrier.wait()
            statuses.append(call("POST", f"/api/cases/cases/{case}/review", lead,
                                 {"decision": decision, "note": "race"})[0])

        threads = [threading.Thread(target=decide, args=(d,)) for d in ("APPROVED", "REJECTED")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        race[case] = sorted(statuses)
    stored_reviews = {case: len(stored(case)["reviews"]) for case in race_cases}
    observed["race_statuses"], observed["race_stored_reviews"] = race, stored_reviews
    checks["concurrent_lead_decisions_record_exactly_one"] = all(
        race[case] == [200, 400] and stored_reviews[case] == 1 for case in race_cases)

    evidence = {
        "generated_at": started.isoformat(timespec="seconds"),
        "what": "Packaged-stack sign-in, roles and PostgreSQL case store through the gateway",
        "compose_project": args.project,
        "passed": sum(checks.values()), "total": len(checks),
        "checks": checks, "observed": observed,
        "method": [
            "Requests go to the gateway on 127.0.0.1:18119 (nginx auth_request to oauth2-proxy).",
            "Demo users sign in with Dex's password grant; the ID token is sent as a bearer "
            "token, which oauth2-proxy verifies against Dex's signing keys.",
            "Forged headers: X-PayGuard-User=mallory, X-PayGuard-Role=lead and the default "
            "local-demo proxy token, sent by the client.",
            f"Race: {RACE_CASES} cases, each with an APPROVED and a REJECTED lead request "
            "released together by a barrier; one cases-api process (FastAPI thread pool).",
        ],
        "limits": [
            "Synthetic events seeded with CaseStore.observe, not from Kafka.",
            "Bearer-token sign-in, not the browser cookie flow (see case_review_stack_browser).",
            "One machine, loopback only; the demo credentials are public placeholders.",
        ],
    }
    RESULT.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": evidence["passed"], "total": evidence["total"],
                      "failed": [name for name, ok in checks.items() if not ok]}, indent=2))
    return 0 if evidence["passed"] == evidence["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
