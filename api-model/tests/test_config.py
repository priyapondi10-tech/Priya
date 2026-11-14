import pytest
from api_model.config import digest, load_json, money, validate


@pytest.mark.parametrize("path,value", [
    (("schema_version",), 1), (("schema_version",), True), (("mode",), "live"),
    (("live_trading_enabled",), True), (("live_trading_enabled",), 0),
    (("capital", "initial_capital"), None), (("capital", "initial_capital"), 200000),
    (("capital", "currency"), "EUR"), (("capital", "max_deployed_value"), 50000),
    (("capital", "max_deployed_pct"), 50), (("capital", "max_deployed_pct"), True),
    (("position_limits", "max_position_value"), 10000), (("position_limits", "max_position_value"), 4999),
    (("asset_policy", "allow_shorts"), True), (("asset_policy", "allow_leverage"), True),
    (("profit_protection", "pullback_min_pct"), 0.9), (("execution", "slippage_bps"), float("nan")),
])
def test_unsafe_configuration(raw, path, value):
    node = raw
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValueError):
        validate(raw, "trading")


def test_requested_limits_and_hash(raw):
    assert raw["capital"] == {"initial_capital": 100000, "currency": "USD", "max_deployed_pct": 25, "max_deployed_value": 25000}
    assert raw["position_limits"] == {"max_position_pct": 5, "max_position_value": 5000}
    assert digest(raw) == digest(validate(raw, "trading"))


def test_unknown_fields_rejected(raw):
    raw["unrecognized_limit"] = 50000
    with pytest.raises(ValueError):
        validate(raw, "trading")


@pytest.mark.parametrize("content", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_strict_json(tmp_path, content):
    path = tmp_path / "config.json"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_json(path)


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", {}, []])
def test_invalid_money(value):
    with pytest.raises(ValueError):
        money(value)


def test_stricter_limits(raw):
    raw["capital"].update(max_deployed_pct=20, max_deployed_value=20000)
    assert validate(raw, "trading")["capital"]["max_deployed_value"] == 20000


def test_training_split_validation(training):
    training.update(train_fraction=0.8, validation_fraction=0.2)
    with pytest.raises(ValueError):
        validate(training, "training")
