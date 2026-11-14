import hashlib
import json

import numpy as np
import pandas as pd
import pytest
from api_model import market
from api_model.learning import chronological_splits, forecast, load_model, train
from api_model.market import (
    FEATURES,
    _wilder,
    dataset,
    feature_frame,
    ingest_csv,
    read_database,
    validate_bars,
)
from sqlalchemy import event
from sqlalchemy.exc import OperationalError


def test_causal_indicators(bars, training):
    original = feature_frame(bars, training)
    changed = bars.copy()
    changed.loc[400:, ["open", "high", "low", "close"]] *= 1.7
    pd.testing.assert_frame_equal(original.loc[:399, FEATURES], feature_frame(changed, training).loc[:399, FEATURES])
    assert original.target_return.iloc[250] == pytest.approx(bars.close.iloc[251] / bars.close.iloc[250] - 1)
    assert pd.isna(original.target_return.iloc[-1])


def test_symbol_isolation(bars, training):
    other = bars.copy()
    other["symbol"] = "BBB"
    other[["open", "high", "low", "close"]] *= 10
    combined = feature_frame(pd.concat([bars, other], ignore_index=True), training)
    np.testing.assert_allclose(combined[combined.symbol == "AAA"][FEATURES], feature_frame(bars, training)[FEATURES], equal_nan=True)


def test_indicator_math(bars, training):
    row = feature_frame(bars, training).iloc[-1]
    assert row.sma_20_distance == pytest.approx(bars.close.iloc[-1] / bars.close.tail(20).mean() - 1)
    assert row.ema_20_distance == pytest.approx(bars.close.iloc[-1] / bars.close.ewm(span=20, adjust=False).mean().iloc[-1] - 1)
    macd = bars.close.ewm(span=12, adjust=False, min_periods=12).mean() - bars.close.ewm(span=26, adjust=False, min_periods=26).mean()
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    assert row.macd == pytest.approx(macd.iloc[-1] / bars.close.iloc[-1])
    assert row.macd_histogram == pytest.approx((macd - signal).iloc[-1] / bars.close.iloc[-1])
    seed = _wilder(pd.Series([np.nan] + list(range(1, 17))))
    assert seed.iloc[14] == 7.5
    assert seed.iloc[15] == pytest.approx((7.5 * 13 + 15) / 14)


@pytest.mark.parametrize("direction,expected", [(1, 1), (0, 0.5), (-1, 0)])
def test_rsi(bars, training, direction, expected):
    bars["close"] = 1000 + direction * np.arange(len(bars))
    bars["open"], bars["high"], bars["low"] = bars.close, bars.close + 1, bars.close - 1
    assert feature_frame(bars, training).rsi_14.iloc[-1] == expected


@pytest.mark.parametrize("kind", ["duplicate", "missing", "negative", "ohlc", "currency", "incomplete", "unadjusted", "nan", "infinite", "naive", "future", "boolean"])
def test_invalid_bars(bars, as_of, kind):
    if kind == "duplicate":
        bars = pd.concat([bars, bars.iloc[[-1]]])
    elif kind == "missing":
        bars = bars.drop(columns="volume")
    elif kind == "negative":
        bars.loc[0, "volume"] = -1
    elif kind == "ohlc":
        bars.loc[0, "high"] = 1
    elif kind == "currency":
        bars.loc[0, "currency"] = "EUR"
    elif kind in ("incomplete", "unadjusted"):
        bars.loc[0, "is_complete" if kind == "incomplete" else "is_adjusted"] = 0
    elif kind in ("nan", "infinite"):
        bars.loc[0, "close"] = np.nan if kind == "nan" else np.inf
    elif kind == "naive":
        bars["timestamp"] = bars.timestamp.dt.tz_localize(None)
    elif kind == "future":
        bars.loc[len(bars) - 1, "timestamp"] = pd.Timestamp(as_of) + pd.Timedelta(days=1)
    else:
        bars["close"] = True
    with pytest.raises((ValueError, TypeError)):
        validate_bars(bars, as_of)


def test_database_roundtrip_is_read_only(bars, as_of, training, tmp_path, monkeypatch):
    csv, db = tmp_path / "bars.csv", tmp_path / "market.sqlite"
    bars.to_csv(csv, index=False)
    assert ingest_csv(csv, db, as_of) == len(bars)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    create = market.create_engine
    blocked = []
    def checked_engine(*args, **kwargs):
        engine = create(*args, **kwargs)
        @event.listens_for(engine, "before_cursor_execute")
        def check(connection, cursor, statement, parameters, context, many):
            if statement.startswith("SELECT symbol"):
                with pytest.raises(OperationalError):
                    connection.exec_driver_sql("DELETE FROM market_bars")
                blocked.append(True)
        return engine
    monkeypatch.setattr(market, "create_engine", checked_engine)
    assert len(read_database(f"sqlite:///{db}", training, as_of)) == len(bars)
    assert blocked and before == hashlib.sha256(db.read_bytes()).hexdigest()


def test_database_errors_hide_credentials(training, as_of):
    with pytest.raises(ValueError) as error:
        read_database("mysql://user:secret-password@invalid/database", training, as_of)
    assert "secret-password" not in str(error.value)


def test_stale_history(bars, as_of):
    with pytest.raises(ValueError, match="Stale"):
        validate_bars(bars.iloc[:-30], as_of, 7)


def test_purged_chronology(bars, training):
    parts = chronological_splits(dataset(bars, training), training)
    assert parts["train"].target_timestamp.max() < parts["validation"].timestamp.min()
    assert parts["validation"].target_timestamp.max() < parts["test"].timestamp.min()
    assert parts["development"].target_timestamp.max() < parts["test"].timestamp.min()


def test_training_archive_forecast_and_corruption(bars, training, config, as_of, tmp_path):
    result = train(bars, training, config, tmp_path, as_of, "test-fixture")
    path = tmp_path / result["run_id"] / "model.json"
    model = load_model(path)
    parts = chronological_splits(dataset(bars, training), training)
    np.testing.assert_allclose(model["scaler_mean"], parts["development"][FEATURES].mean().to_numpy())
    assert model["live_deployment"] is False and len(model["coefficients"]) == len(FEATURES)
    archive = json.loads((tmp_path / "parameter-archive.jsonl").read_text())
    assert archive["coefficients"] == model["coefficients"]
    assert archive["model_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert forecast(bars, model, as_of)[0]["order_submitted"] is False
    with pytest.raises(ValueError, match="cutoff"):
        forecast(bars.iloc[:420], model, bars.timestamp.iloc[419].to_pydatetime())
    with pytest.raises(FileExistsError):
        train(bars, training, config, tmp_path, as_of, "test-fixture")
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="checksum"):
        load_model(path)


def test_insufficient_training(bars, training, config, as_of, tmp_path):
    with pytest.raises(ValueError, match="Insufficient"):
        train(bars.tail(100), training, config, tmp_path, as_of)


def test_failed_quality_gate_never_promotes(bars, training, config, as_of, tmp_path):
    training["gate"]["maximum_test_mae_ratio"] = 0.01
    result = train(bars, training, config, tmp_path, as_of)
    assert result["eligible_for_paper_evaluation"] is False
    assert load_model(tmp_path / result["run_id"] / "model.json")["live_deployment"] is False
