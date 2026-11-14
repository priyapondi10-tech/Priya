import copy
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from api_model.config import load_config, validate

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def raw():
    return load_config(ROOT / "config/trading.example.json")


@pytest.fixture
def config(raw):
    raw = copy.deepcopy(raw)
    raw["asset_policy"]["allowed_symbols"] = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    raw["execution"].update(commission_bps=0, slippage_bps=0)
    return validate(raw, "trading")


@pytest.fixture
def training():
    return load_config(ROOT / "api-model/config/training.json", "training")


@pytest.fixture
def now():
    return datetime(2025, 1, 6, 15, tzinfo=timezone.utc)


@pytest.fixture
def quote(now):
    def make(symbol="AAA", price="100", high=None, at=None):
        return {"symbol": symbol, "price": price, "session_high": high or price,
                "timestamp": (at or now).isoformat(), "currency": "USD", "asset_class": "equity"}
    return make


@pytest.fixture
def bars():
    generator = np.random.default_rng(42)
    n = 620
    close = 80 * np.exp(np.cumsum(0.001 + 0.006 * np.sin(np.arange(n) / 9) + generator.normal(0, 0.001, n)))
    opening = np.r_[close[0], close[:-1]] * (1 + generator.normal(0, 0.0003, n))
    return pd.DataFrame({"symbol": "AAA", "timestamp": pd.bdate_range(end="2025-01-03", periods=n, tz="UTC") + pd.Timedelta(hours=21),
        "open": opening, "high": np.maximum(opening, close) * 1.002, "low": np.minimum(opening, close) * 0.998,
        "close": close, "volume": 100000 + np.arange(n) % 300, "currency": "USD", "is_complete": 1, "is_adjusted": 1})


@pytest.fixture
def as_of():
    return datetime(2025, 1, 3, 22, tzinfo=timezone.utc)
