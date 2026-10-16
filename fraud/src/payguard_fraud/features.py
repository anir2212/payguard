"""Causal online features: every row uses only the current request and prior rows."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from payguard_fraud.simulator import Event

FEATURES = (
    "log_amount", "amount_z", "seconds_since_last", "home_distance_km",
    "last_distance_km", "mcc_novel", "device_novel", "merchant_risk",
    "count_1m", "count_1h", "count_24h", "small_auths_1m",
    "prior_refund_ratio", "hour_sin", "hour_cos",
)


def distance_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    lat1, lat2 = math.radians(a_lat), math.radians(b_lat)
    dlat = lat2 - lat1
    dlon = math.radians(b_lon - a_lon)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(value)))


@dataclass
class CardHistory:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    last_time: object | None = None
    last_location: tuple[float, float] | None = None
    mccs: set[str] = field(default_factory=set)
    devices: set[str] = field(default_factory=set)
    recent: deque = field(default_factory=deque)


class FeatureBuilder:
    def __init__(self) -> None:
        self.cards: dict[str, CardHistory] = defaultdict(CardHistory)

    def transform_one(self, event: Event, *, update: bool = True) -> dict[str, float]:
        history = self.cards[event.card_id]
        while history.recent and history.recent[0][0] < event.ts - timedelta(hours=24):
            history.recent.popleft()
        amount = float(event.amount_minor)
        variance = history.m2 / (history.count - 1) if history.count > 1 else 0.0
        deviation = math.sqrt(max(variance, 1.0))
        z_score = (amount - history.mean) / deviation if history.count >= 5 else 0.0
        since_last = (
            (event.ts - history.last_time).total_seconds() if history.last_time else 86_400.0
        )
        previous_location = history.last_location or (event.home_latitude, event.home_longitude)
        one_minute = event.ts - timedelta(minutes=1)
        one_hour = event.ts - timedelta(hours=1)
        hour = event.ts.hour + event.ts.minute / 60
        result = {
            "log_amount": math.log1p(amount),
            "amount_z": max(-20.0, min(20.0, z_score)),
            "seconds_since_last": min(86_400.0, max(0.0, since_last)),
            "home_distance_km": distance_km(event.home_latitude, event.home_longitude,
                                            event.latitude, event.longitude),
            "last_distance_km": distance_km(*previous_location, event.latitude, event.longitude),
            "mcc_novel": float(event.mcc not in history.mccs),
            "device_novel": float(event.device_id not in history.devices),
            "merchant_risk": event.merchant_risk,
            "count_1m": float(sum(ts >= one_minute for ts, _ in history.recent)),
            "count_1h": float(sum(ts >= one_hour for ts, _ in history.recent)),
            "count_24h": float(len(history.recent)),
            "small_auths_1m": float(sum(ts >= one_minute and value < 300
                                          for ts, value in history.recent)),
            "prior_refund_ratio": event.prior_refund_ratio,
            "hour_sin": math.sin(2 * math.pi * hour / 24),
            "hour_cos": math.cos(2 * math.pi * hour / 24),
        }
        # Update after computing: the current event must not enter its own history.
        if update:
            history.count += 1
            delta = amount - history.mean
            history.mean += delta / history.count
            history.m2 += delta * (amount - history.mean)
            history.last_time = event.ts
            history.last_location = (event.latitude, event.longitude)
            history.mccs.add(event.mcc)
            history.devices.add(event.device_id)
            history.recent.append((event.ts, event.amount_minor))
        return result


def history_to_dict(history: CardHistory) -> dict:
    return {
        "count": history.count,
        "mean": history.mean,
        "m2": history.m2,
        "last_time": history.last_time.isoformat() if history.last_time else None,
        "last_location": history.last_location,
        "mccs": sorted(history.mccs),
        "devices": sorted(history.devices),
        "recent": [(ts.isoformat(), amount) for ts, amount in history.recent],
    }


def history_from_dict(value: dict) -> CardHistory:
    return CardHistory(
        count=value["count"], mean=value["mean"], m2=value["m2"],
        last_time=datetime.fromisoformat(value["last_time"]) if value["last_time"] else None,
        last_location=tuple(value["last_location"]) if value["last_location"] else None,
        mccs=set(value["mccs"]), devices=set(value["devices"]),
        recent=deque((datetime.fromisoformat(ts), amount) for ts, amount in value["recent"]),
    )


def build_frame(events: list[Event]) -> pd.DataFrame:
    builder = FeatureBuilder()
    rows = []
    for event in events:
        features = builder.transform_one(event)
        rows.append({**features, "transaction_id": event.transaction_id,
                     "timestamp": event.ts, "amount_minor": event.amount_minor,
                     "label": event.label, "fraud_type": event.fraud_type})
    return pd.DataFrame(rows)
