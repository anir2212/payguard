"""At-least-once Kafka-to-case-store consumer for high-scoring authorizations."""

from __future__ import annotations

import json
import os
from pathlib import Path

from confluent_kafka import Consumer

from payguard_fraud.case_store import CaseStore


def main() -> None:
    store = CaseStore(Path(os.getenv("PAYGUARD_CASE_DB", "data/cases.sqlite")))
    consumer = Consumer({
        "bootstrap.servers": os.getenv("PAYGUARD_KAFKA_BROKERS", "127.0.0.1:19094"),
        "group.id": "payguard-cases-v1", "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe(["payguard.authorization"])
    try:
        while True:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                raise RuntimeError(str(message.error()))
            store.observe(json.loads(message.value()))
            consumer.commit(message=message, asynchronous=False)
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
