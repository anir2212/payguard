"""The simulator signs in for the case API only when the stack asks it to."""

from __future__ import annotations

import base64
import io
import json
from urllib.parse import parse_qs

from payguard_fraud import demo


def test_no_token_url_sends_no_credentials(monkeypatch) -> None:
    monkeypatch.delenv("PAYGUARD_CASE_TOKEN_URL", raising=False)
    assert demo.case_api_headers() == {}


def test_password_grant_returns_bearer_id_token(monkeypatch) -> None:
    for name, value in {"PAYGUARD_CASE_TOKEN_URL": "http://dex:5556/dex/token",
                        "PAYGUARD_CASE_CLIENT_ID": "client", "PAYGUARD_CASE_CLIENT_SECRET": "s",
                        "PAYGUARD_CASE_USER": "analyst@payguard.local",
                        "PAYGUARD_CASE_PASSWORD": "pw"}.items():
        monkeypatch.setenv(name, value)
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"], seen["form"] = request.full_url, parse_qs(request.data.decode())
        seen["auth"] = request.get_header("Authorization")
        return io.BytesIO(json.dumps({"id_token": "header.claims.signature",
                                      "access_token": "unused"}).encode())

    monkeypatch.setattr(demo, "urlopen", fake_urlopen)
    assert demo.case_api_headers() == {"Authorization": "Bearer header.claims.signature"}
    assert seen["url"] == "http://dex:5556/dex/token"
    assert seen["form"]["grant_type"] == ["password"]
    assert seen["form"]["username"] == ["analyst@payguard.local"]
    assert "openid" in seen["form"]["scope"][0].split()
    assert base64.b64decode(seen["auth"].removeprefix("Basic ")) == b"client:s"
