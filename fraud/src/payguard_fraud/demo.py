"""Exercise a card-testing burst across the API, outbox, Kafka, and Redis.

Run only against the local PayGuard stack. The output records observed decisions
and feature propagation; it does not prescribe a particular model outcome.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import redis

from payguard_fraud.score_api import FeatureStore
from payguard_fraud.simulator import Event


def send(url: str, payload: dict, method: str = "POST", headers: dict | None = None) -> dict:
    request = Request(url, json.dumps(payload).encode(),
                      {"Content-Type": "application/json", **(headers or {})}, method=method)
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def case_api_headers() -> dict:
    """Sign in for the case API when the stack puts it behind the gateway.

    With ``PAYGUARD_CASE_TOKEN_URL`` set, uses the identity provider's password
    grant for ``PAYGUARD_CASE_USER`` and sends the ID token as a bearer token,
    which the gateway's oauth2-proxy verifies. Without it, sends nothing (a
    case API in local mode needs no sign-in).
    """
    token_url = os.getenv("PAYGUARD_CASE_TOKEN_URL")
    if not token_url:
        return {}
    client = f"{os.environ['PAYGUARD_CASE_CLIENT_ID']}:{os.environ['PAYGUARD_CASE_CLIENT_SECRET']}"
    form = urlencode({"grant_type": "password", "scope": "openid email profile",
                      "username": os.environ["PAYGUARD_CASE_USER"],
                      "password": os.environ["PAYGUARD_CASE_PASSWORD"]}).encode()
    request = Request(token_url, form, {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "Basic " + base64.b64encode(client.encode()).decode()})
    with urlopen(request, timeout=10) as response:
        return {"Authorization": f"Bearer {json.load(response)['id_token']}"}


def wait_for_observation(client: redis.Redis, auth_id: str, timeout: float = 10.0) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if client.exists(f"payguard:seen:{auth_id}"):
            return round((time.monotonic() - start) * 1_000, 2)
        time.sleep(0.02)
    raise TimeoutError(f"Kafka feature consumer did not observe {auth_id}")


def main() -> None:
    api = os.getenv("PAYGUARD_API_URL", "http://127.0.0.1:8086")
    client = redis.Redis.from_url(os.getenv("PAYGUARD_REDIS_URL", "redis://127.0.0.1:16379/0"))
    client.ping()
    nonce = uuid4().hex[:10]
    issued = send(f"{api}/v1/cards", {
        "companyId": f"demo_{nonce}", "holder": "Synthetic Test Holder",
        "currency": "SGD", "limitMinor": 1_000_000,
        "idempotencyKey": f"demo_issue_{nonce}",
    })
    card_id = issued["reference"]
    send(f"{api}/v1/cards/{card_id}/state", {
        "state": "ACTIVE", "idempotencyKey": f"demo_active_{nonce}",
    }, "PATCH")
    send(f"{api}/v1/cards/{card_id}/controls", {
        "controls": {"velocityCount": 1000, "velocitySeconds": 60},
        "idempotencyKey": f"demo_controls_{nonce}",
    }, "PUT")

    # Seed prior normal behaviour in the feature store only. A real deployment
    # would rebuild this state from its event stream before serving scores.
    store = FeatureStore(client)
    now = datetime.now(UTC)
    for index in range(20):
        store.observe(Event(
            f"seed_{nonce}_{index}", now - timedelta(hours=23) + timedelta(minutes=index * 40),
            card_id, "merchant_grocery", "5411", 3000 + index * 50,
            f"device_known_{nonce}", 1.35, 103.82, 1.35, 103.82, 0.08, 0.01, 0, None,
        ))

    decisions = []
    for index in range(8):
        auth_id = f"auth_demo_{nonce}_{index}"
        started = time.monotonic()
        result = send(f"{api}/v1/authorizations", {
            "authId": auth_id, "cardId": card_id, "merchantId": "merchant_test",
            "mcc": "5411", "country": "SG", "currency": "SGD",
            "amountMinor": 100 + index * 5, "deviceId": f"device_new_{nonce}",
            "merchantRisk": 0.08, "priorRefundRatio": 0.01,
            "idempotencyKey": f"demo_auth_{nonce}_{index}",
        })
        api_ms = round((time.monotonic() - started) * 1_000, 2)
        propagation_ms = wait_for_observation(client, auth_id)
        decisions.append({
            "authId": auth_id, "status": result["status"],
            "modelScore": result.get("modelScore"),
            "modelAction": result.get("modelAction"),
            "reasons": result.get("reasons", []),
            "modelReasonCodes": result.get("modelReasonCodes", []),
            "apiMs": api_ms, "featurePropagationMs": propagation_ms,
        })
    report = {
        "scenario": "synthetic card-testing burst with 20 normal prior transactions",
        "cardId": card_id, "decisions": decisions,
        "firstModelDeclineIndex": next((i for i, row in enumerate(decisions)
                                        if row["modelAction"] == "MODEL_DECLINE"), None),
    }
    case_api = os.getenv("PAYGUARD_CASE_API_URL")
    if case_api:
        case_id = f"case_{decisions[-1]['authId']}"
        headers = case_api_headers()
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(Request(f"{case_api}/cases/{case_id}", headers=headers), timeout=5):
                    break
            except HTTPError as error:
                if error.code != 404 or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        report["caseDraft"] = send(f"{case_api}/cases/{case_id}/draft", {}, headers=headers)
    target = Path(os.getenv("PAYGUARD_DEMO_REPORT", "bench/burst_results.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
