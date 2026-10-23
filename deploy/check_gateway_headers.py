"""Show which request headers the gateway passes to the services.

Starts deploy/gateway.conf.template in nginx on a throwaway Docker network
next to one stand-in container that answers for every upstream name: it plays
oauth2-proxy (202 plus an email for the cookie ``session=analyst`` or
``session=visitor``, otherwise 401) and echoes the headers and body length it
receives as the console, the payments API and the case API. Needs Docker and
the nginx and Python images named below; removes its containers and network.

    python3 deploy/check_gateway_headers.py

Writes bench/gateway_header_check.json.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "bench" / "gateway_header_check.json"
NGINX, PYTHON = "nginx:1.27.2-alpine", "python:3.12-slim"
NAME, PORT, TOKEN = "p09-gateway-check", 18129, "check-only-token"

STAND_IN = r'''
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
EMAILS = {"session=analyst": "analyst@payguard.local", "session=visitor": "visitor@payguard.local"}
class Echo(BaseHTTPRequestHandler):
    def reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/oauth2/auth"):
            email = EMAILS.get(self.headers.get("Cookie", ""))
            self.send_response(202 if email else 401)
            if email:
                self.send_header("X-Auth-Request-Email", email)
            self.end_headers()
            return
        out = json.dumps({"port": self.server.server_port, "path": self.path,
                          "headers": {k.lower(): v for k, v in self.headers.items()},
                          "body_bytes": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    do_GET = do_POST = reply
    def log_message(self, *args):
        pass
for port in (80, 4180, 5556, 8086, 18087):
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", port), Echo).serve_forever).start()
'''


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, capture_output=True,
                          text=True).stdout.strip()


def request(path: str, headers: dict, body: bytes | None = None) -> tuple[int, dict | str]:
    method = "POST" if body is not None else "GET"
    sent = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", body, headers, method=method)
    try:
        with urllib.request.urlopen(sent, timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, error.read().decode()[:200]


def main() -> int:
    docker("network", "create", NAME)
    try:
        docker("run", "-d", "--name", f"{NAME}-upstreams", "--network", NAME,
               *[arg for alias in ("console", "dex", "oauth2-proxy", "api", "cases-api")
                 for arg in ("--network-alias", alias)],
               PYTHON, "python", "-c", STAND_IN)
        docker("run", "-d", "--name", f"{NAME}-gateway", "--network", NAME,
               "-e", f"PAYGUARD_PROXY_TOKEN={TOKEN}", "-p", f"127.0.0.1:{PORT}:80",
               "-v", f"{ROOT / 'deploy' / 'gateway.conf.template'}:"
                     "/etc/nginx/templates/default.conf.template:ro", NGINX)
        # Ready once a signed-out request gets the stand-in's 401 (not 502 or refused).
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/cases/me", timeout=1)
            except HTTPError as error:
                if error.code == 401:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        forged = {"X-PayGuard-User": "mallory", "X-PayGuard-Role": "lead",
                  "X-PayGuard-Proxy-Token": "guessed", "X-PayGuard-Extra": "anything",
                  "X-Payguard-Role-Override": "lead", "Authorization": "Bearer client-token",
                  "X-Forwarded-User": "mallory", "Accept": "application/json"}
        checks: dict[str, bool] = {}
        observed: dict = {}

        status, _ = request("/api/cases/me", forged)
        checks["no_session_401_before_upstream"] = status == 401
        status, cases = request("/api/cases/me", {**forged, "Cookie": "session=analyst"})
        headers = cases["headers"] if status == 200 else {}
        observed["case_api_received"] = headers
        checks["case_api_gets_gateway_identity"] = (
            headers.get("x-payguard-user") == "analyst@payguard.local"
            and headers.get("x-payguard-role") == "analyst"
            and headers.get("x-payguard-proxy-token") == TOKEN)
        checks["case_api_gets_no_other_x_payguard_header"] = sorted(
            name for name in headers if name.startswith("x-payguard")) == [
            "x-payguard-proxy-token", "x-payguard-role", "x-payguard-user"]
        checks["case_api_gets_no_cookie_or_authorization"] = not (
            {"cookie", "authorization", "x-forwarded-user"} & set(headers))
        review = b'{"decision":"APPROVED","note":"x"}'
        status, posted = request("/api/cases/cases/c1/review",
                                 {**forged, "Cookie": "session=analyst",
                                  "Content-Type": "application/json"},
                                 review)
        checks["post_body_and_content_type_pass"] = status == 200 and (
            posted["body_bytes"] == len(review)
            and posted["headers"].get("content-type") == "application/json"
            and posted["path"] == "/cases/c1/review")
        status, visitor = request("/api/cases/me", {**forged, "Cookie": "session=visitor"})
        observed["unmapped_email_received"] = visitor["headers"] if status == 200 else visitor
        checks["unmapped_email_gets_no_role_header"] = status == 200 and (
            "x-payguard-role" not in visitor["headers"])
        status, payments = request("/api/payments/v1/authorizations/recent",
                                   {**forged, "Cookie": "session=analyst"})
        observed["payments_api_received"] = payments["headers"] if status == 200 else payments
        checks["payments_api_gets_no_x_payguard_header"] = status == 200 and not any(
            name.startswith("x-payguard") for name in payments["headers"])
        evidence = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "what": "Headers the gateway config passes upstream, against stand-in services",
            "images": {"nginx": NGINX, "stand_in": PYTHON},
            "client_sent": {k: v for k, v in forged.items()},
            "passed": sum(checks.values()), "total": len(checks),
            "checks": checks, "observed": observed,
            "limits": [("Stand-in upstreams, not the real oauth2-proxy or APIs; the real "
                        "stack is covered by identity_stack_check.json."),
                       "The proxy token here is a throwaway value for this check."],
        }
        RESULT.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"passed": evidence["passed"], "total": evidence["total"],
                          "failed": [k for k, ok in checks.items() if not ok]}, indent=2))
        return 0 if evidence["passed"] == evidence["total"] else 1
    finally:
        docker("rm", "-f", f"{NAME}-gateway", f"{NAME}-upstreams", check=False)
        docker("network", "rm", NAME, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
