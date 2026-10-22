"""At-least-once Kafka consumer that updates Redis once per auth event ID."""

from __future__ import annotations

import json
import os

import redis
from confluent_kafka import Consumer

from payguard_fraud.score_api import FeatureStore, ScoreRequest, to_event

BATCH = 500


def main() -> None:
    broker = os.getenv("PAYGUARD_KAFKA_BROKERS", "127.0.0.1:19092")
    client = redis.Redis.from_url(os.getenv("PAYGUARD_REDIS_URL", "redis://127.0.0.1:16379/0"))
    store = FeatureStore(client)
    consumer = Consumer({
        "bootstrap.servers": broker,
        "group.id": "payguard-features-v1",
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe(["payguard.authorization"])
    try:
        while True:
            # One synchronous commit per batch, after every event in it is applied.
            # A crash mid-batch replays the batch; observe() skips seen event IDs.
            messages = consumer.consume(num_messages=BATCH, timeout=1.0)
            for message in messages:
                if message.error():
                    raise RuntimeError(str(message.error()))
                payload = json.loads(message.value())
                request = ScoreRequest.model_validate(payload["request"] | {
                    "timestamp": payload["timestamp"]
                })
                store.observe(to_event(request))
            if messages:
                consumer.commit(asynchronous=False)
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
