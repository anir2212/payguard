"""At-least-once Kafka-to-case-store consumer for high-scoring authorizations."""

from __future__ import annotations

import json
import os

from confluent_kafka import Consumer

from payguard_fraud.case_store import CaseStore

BATCH = 500


def main() -> None:
    store = CaseStore.from_env()
    consumer = Consumer({
        "bootstrap.servers": os.getenv("PAYGUARD_KAFKA_BROKERS", "127.0.0.1:19094"),
        "group.id": "payguard-cases-v1", "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe(["payguard.authorization"])
    try:
        while True:
            # One synchronous commit per batch, after every message in it is stored.
            # A crash mid-batch replays the batch; observe() ignores repeated auth IDs.
            messages = consumer.consume(num_messages=BATCH, timeout=1.0)
            for message in messages:
                if message.error():
                    raise RuntimeError(str(message.error()))
                store.observe(json.loads(message.value()))
            if messages:
                consumer.commit(asynchronous=False)
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
