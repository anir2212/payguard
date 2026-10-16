"""Deterministic synthetic authorizations with labelled fraud patterns.

All identities are invented tokens. Labels are produced by the simulator, never by
real customer information. The patterns are intentionally learnable and should not
be mistaken for real-world model validity.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

CITIES = {
    "SG": (1.35, 103.82),
    "KL": (3.14, 101.69),
    "HK": (22.32, 114.17),
    "TYO": (35.68, 139.69),
    "LON": (51.51, -0.13),
    "NYC": (40.71, -74.01),
}
MCCS = ("5411", "5812", "5732", "4814", "7995", "5942")
PATTERNS = ("card_testing", "account_takeover", "merchant_collusion",
            "refund_abuse", "velocity_burst")


@dataclass(frozen=True)
class Event:
    transaction_id: str
    ts: datetime
    card_id: str
    merchant_id: str
    mcc: str
    amount_minor: int
    device_id: str
    latitude: float
    longitude: float
    home_latitude: float
    home_longitude: float
    merchant_risk: float
    prior_refund_ratio: float
    label: int
    fraud_type: str | None


def generate(count: int = 60_000, seed: int = 11) -> list[Event]:
    """Return time-sorted events over roughly 60 days; bursts add extra records."""
    if count < 1:
        raise ValueError("count must be positive")
    rng = random.Random(seed)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    profiles = {}
    for card in range(500):
        city = rng.choice(tuple(CITIES))
        home = CITIES[city]
        profiles[f"card_{card:05d}"] = {
            "home": home,
            "typical": rng.randint(900, 12_000),
            "device": f"device_{card:05d}",
            "mcc": rng.choice(MCCS),
            "refund_ratio": rng.uniform(0.35, 0.75) if card % 29 == 0 else rng.uniform(0, 0.12),
        }
    card_ids = tuple(profiles)
    merchant_risks = {f"merchant_{index:03d}": rng.uniform(0.02, 0.3)
                      for index in range(100)}
    merchant_risks["merchant_ring_01"] = 0.9
    events = []
    next_id = 0

    def append(ts, card_id, merchant, mcc, amount, device, city, pattern=None):
        nonlocal next_id
        profile = profiles[card_id]
        lat, lon = CITIES[city]
        events.append(Event(
            f"txn_{next_id:09d}", ts, card_id, merchant, mcc, max(1, int(amount)),
            device, lat, lon, *profile["home"], merchant_risks[merchant], profile["refund_ratio"],
            int(pattern is not None), pattern,
        ))
        next_id += 1

    for i in range(count):
        ts = start + timedelta(seconds=i * 86.4 + rng.uniform(-8, 8))
        card_id = rng.choice(card_ids)
        profile = profiles[card_id]
        home_city = next(city for city, coords in CITIES.items() if coords == profile["home"])
        amount = max(100, int(rng.lognormvariate(math.log(profile["typical"]), 0.55)))
        merchant = f"merchant_{rng.randrange(100):03d}"
        mcc = profile["mcc"] if rng.random() < 0.75 else rng.choice(MCCS)
        if rng.random() >= 0.018:
            append(ts, card_id, merchant, mcc, amount, profile["device"], home_city)
            continue
        pattern = rng.choice(PATTERNS)
        if pattern in {"card_testing", "velocity_burst"}:
            for offset in range(6):
                burst_amount = rng.randint(40, 250) if pattern == "card_testing" else amount * 3
                append(ts + timedelta(seconds=offset * 5), card_id, merchant, mcc, burst_amount,
                       f"device_new_{card_id}", home_city, pattern)
        elif pattern == "account_takeover":
            distant = rng.choice(tuple(city for city in CITIES if city != home_city))
            append(ts, card_id, merchant, mcc, amount * 8,
                   f"device_new_{card_id}_{i}", distant, pattern)
        elif pattern == "merchant_collusion":
            append(ts, card_id, "merchant_ring_01", "7995", amount * 3,
                   profile["device"], home_city, pattern)
        else:
            abuser = f"card_{rng.randrange(0, 500 // 29) * 29:05d}"
            abuser_profile = profiles[abuser]
            abuser_city = next(city for city, coords in CITIES.items()
                               if coords == abuser_profile["home"])
            append(ts, abuser, merchant, mcc, amount * 2,
                   abuser_profile["device"], abuser_city, pattern)
    return sorted(events, key=lambda event: (event.ts, event.transaction_id))
