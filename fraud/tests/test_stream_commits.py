"""Consumers commit once per batch, and only after every message in it is handled."""

from __future__ import annotations

import json

import pytest

from payguard_fraud import case_stream, feature_stream


class Message:
    def __init__(self, payload: dict):
        self.payload = payload

    def error(self):
        return None

    def value(self) -> bytes:
        return json.dumps(self.payload).encode()


class FakeConsumer:
    """Returns the given batches, then stops the loop."""

    def __init__(self, batches: list[list[Message]], log: list):
        self.batches, self.log = list(batches), log

    def subscribe(self, topics: list[str]) -> None:
        pass

    def consume(self, num_messages: int, timeout: float) -> list[Message]:
        if not self.batches:
            raise KeyboardInterrupt
        return self.batches.pop(0)

    def commit(self, asynchronous: bool) -> None:
        assert asynchronous is False
        self.log.append("commit")

    def close(self) -> None:
        self.log.append("close")


def event(index: int) -> dict:
    request = {"authId": f"auth_{index}", "cardId": "card_1", "merchantId": "m1", "mcc": "5411",
               "amountMinor": 1000, "deviceId": "d1"}
    return {"request": request, "result": {"modelScore": 0.5},
            "timestamp": "2026-10-22T03:00:00Z"}


def run(module, monkeypatch, batches, observe) -> list:
    log: list = []
    monkeypatch.setattr(module, "Consumer", lambda config: FakeConsumer(batches, log))
    store = type("Store", (), {"observe": lambda self, item: observe(log, item)})()
    if module is case_stream:
        monkeypatch.setattr(case_stream.CaseStore, "from_env", classmethod(lambda cls: store))
    else:
        monkeypatch.setattr(feature_stream.redis.Redis, "from_url", lambda url: None)
        monkeypatch.setattr(feature_stream, "FeatureStore", lambda client: store)
    with pytest.raises((KeyboardInterrupt, RuntimeError)):
        module.main()
    return log


@pytest.mark.parametrize("module", [case_stream, feature_stream])
def test_one_commit_per_batch_after_its_messages(module, monkeypatch) -> None:
    batches = [[Message(event(0)), Message(event(1)), Message(event(2))], [], [Message(event(3))]]
    log = run(module, monkeypatch, batches, lambda log, item: log.append("observe"))
    assert log == ["observe"] * 3 + ["commit", "observe", "commit", "close"]


@pytest.mark.parametrize("module", [case_stream, feature_stream])
def test_failed_message_leaves_its_batch_uncommitted(module, monkeypatch) -> None:
    def observe(log, item):
        if len(log) == 1:
            raise RuntimeError("store unavailable")
        log.append("observe")

    log = run(module, monkeypatch, [[Message(event(0)), Message(event(1))]], observe)
    assert "commit" not in log and log[-1] == "close"
