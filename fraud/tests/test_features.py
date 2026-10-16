from payguard_fraud.features import FEATURES, FeatureBuilder
from payguard_fraud.metrics import cost_cents, threshold_for_fpr
from payguard_fraud.simulator import generate


def test_simulator_is_reproducible_and_uses_fake_ids():
    first = generate(100, seed=4)
    assert first == generate(100, seed=4)
    assert all(event.card_id.startswith("card_") and event.transaction_id.startswith("txn_")
               for event in first)


def test_online_features_cannot_see_future_events():
    events = generate(1_000, seed=5)
    prefix = events[:100]
    short_builder = FeatureBuilder()
    short = [short_builder.transform_one(event) for event in prefix]
    long_builder = FeatureBuilder()
    full_prefix = [long_builder.transform_one(event) for event in events][:100]
    assert short == full_prefix
    assert set(short[0]) == set(FEATURES)
    assert short[0]["count_1m"] == 0


def test_cost_curve_and_fpr_thresholds():
    labels = [0, 0, 1, 1]
    scores = [0.1, 0.2, 0.8, 0.9]
    amounts = [100, 100, 1_000, 1_000]
    assert cost_cents(labels, scores, amounts, 0.8) == 400
    assert threshold_for_fpr(labels, scores, 0.01) >= 0.2
