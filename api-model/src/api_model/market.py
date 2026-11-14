"""Read-only market access, strict adjusted daily bars, and causal candle indicators."""

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

COLUMNS = ["symbol", "timestamp", "open", "high", "low", "close", "volume", "currency", "is_complete", "is_adjusted"]
FEATURES = ["return_1", "return_5", "sma_20_distance", "ema_20_distance", "rsi_14", "macd", "macd_signal", "macd_histogram", "volatility_20", "volume_ratio_20", "body", "upper_wick", "lower_wick", "doji", "bullish_engulfing", "bearish_engulfing"]


def utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Timezone required")
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Expected a timezone-aware ISO 8601 timestamp") from None


def iso(value):
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_bars(source, as_of, max_age_days=None):
    if source.empty or set(source.columns) != set(COLUMNS):
        raise ValueError(f"Non-empty bars require exactly these columns: {COLUMNS}")
    bars = source[COLUMNS].copy()
    if bars.isna().any().any():
        raise ValueError("Missing data; implicit forward filling is forbidden")
    if not bars.symbol.map(lambda s: isinstance(s, str) and bool(re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", s))).all():
        raise ValueError("Invalid symbol")
    bars["timestamp"] = pd.to_datetime(bars.timestamp.map(utc), utc=True)
    if (bars.timestamp > pd.Timestamp(utc(as_of))).any():
        raise ValueError("Future or not-yet-closed bars")
    if not bars.currency.eq("USD").all():
        raise ValueError("Only USD bars are supported; FX conversion is not implemented")
    for flag in ("is_complete", "is_adjusted"):
        if not bars[flag].isin([True, 1]).all():
            raise ValueError(f"All bars require {flag}=1")
        bars[flag] = 1
    for column in ("open", "high", "low", "close", "volume"):
        if bars[column].map(lambda v: isinstance(v, (bool, np.bool_))).any():
            raise ValueError("Boolean price or volume")
        bars[column] = pd.to_numeric(bars[column], errors="raise").astype(float)
        if not np.isfinite(bars[column]).all():
            raise ValueError("Non-finite market value")
    if (bars[["open", "high", "low", "close"]] <= 0).any().any() or (bars.volume < 0).any():
        raise ValueError("Prices must be positive and volume nonnegative")
    if (bars.high < bars[["open", "close", "low"]].max(axis=1)).any() or (bars.low > bars[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("Inconsistent OHLC range")
    if bars.assign(session_date=bars.timestamp.dt.date).duplicated(["symbol", "session_date"]).any():
        raise ValueError("Duplicate symbol/session daily bar")
    if max_age_days is not None and (bars.groupby("symbol").timestamp.max() < pd.Timestamp(utc(as_of)) - timedelta(days=max_age_days)).any():
        raise ValueError("Stale historical data")
    return bars.sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def read_database(url_value, config, as_of):
    limit = config["max_source_rows"]
    try:
        url = make_url(url_value)
        if url.drivername in {"sqlite", "sqlite+pysqlite"}:
            if not url.database or not Path(url.database).is_file():
                raise ValueError("SQLite database must already exist")
            path = Path(url.database).resolve().as_posix()
            url = make_url(f"sqlite+pysqlite:///file:{path}?mode=ro&uri=true")
            connect_args = {}
        elif url.drivername in {"postgresql", "postgresql+psycopg"}:
            if url.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
                raise ValueError("PostgreSQL requires TLS")
            url = url.set(drivername="postgresql+psycopg")
            connect_args = {"connect_timeout": 15}
        else:
            raise ValueError("Unsupported database dialect")
        engine = create_engine(url, hide_parameters=True, echo=False, connect_args=connect_args)
        try:
            with engine.connect() as connection, connection.begin():
                if url.get_backend_name() == "sqlite":
                    connection.exec_driver_sql("PRAGMA query_only=ON")
                else:
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    connection.exec_driver_sql("SET LOCAL statement_timeout='60000'")
                bars = pd.read_sql_query(text("SELECT symbol,timestamp,open,high,low,close,volume,currency,is_complete,is_adjusted FROM market_bars WHERE is_complete=TRUE AND is_adjusted=TRUE AND currency=:currency ORDER BY timestamp,symbol LIMIT :row_limit"), connection, params={"row_limit": limit + 1, "currency": "USD"})
        finally:
            engine.dispose()
    except Exception:
        raise ValueError("Market database read failed; check a read-only SQLite or TLS PostgreSQL URL and the market_bars schema") from None
    if len(bars) > limit:
        raise ValueError("Source exceeds max_source_rows; provide a bounded database view")
    return validate_bars(bars, as_of, config["max_data_age_days"])


def ingest_csv(csv_path, database, as_of):
    bars = validate_bars(pd.read_csv(csv_path), as_of)
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS market_bars (symbol TEXT NOT NULL,timestamp TEXT NOT NULL,open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,close REAL NOT NULL,volume REAL NOT NULL,currency TEXT NOT NULL CHECK(currency='USD'),is_complete INTEGER NOT NULL CHECK(is_complete=1),is_adjusted INTEGER NOT NULL CHECK(is_adjusted=1),PRIMARY KEY(symbol,timestamp))")
        rows = bars.copy()
        rows["timestamp"] = rows.timestamp.map(iso)
        connection.executemany("INSERT INTO market_bars VALUES (?,?,?,?,?,?,?,?,?,?)", list(rows.itertuples(index=False, name=None)))
    return len(bars)


def _wilder(series, period=14):
    values = series.to_numpy(dtype=float)
    output = np.full(len(values), np.nan)
    if len(values) > period:
        output[period] = np.mean(values[1:period + 1])
        for index in range(period + 1, len(values)):
            output[index] = (output[index - 1] * (period - 1) + values[index]) / period
    return pd.Series(output, index=series.index)


def feature_frame(bars, config):
    result = []
    for _, group in bars.groupby("symbol", sort=True):
        group = group.sort_values("timestamp").copy()
        close, opening, high, low = (group[name] for name in ("close", "open", "high", "low"))
        returns = close.pct_change(fill_method=None)
        macd = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(span=26, adjust=False, min_periods=26).mean()
        signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
        delta = close.diff()
        gain, loss = _wilder(delta.clip(lower=0)), _wilder(-delta.clip(upper=0))
        rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        rsi = rsi.mask((loss == 0) & (gain > 0), 100).mask((loss == 0) & (gain == 0), 50)
        values = [returns, close.pct_change(5, fill_method=None), close / close.rolling(20).mean() - 1,
                  close / close.ewm(span=20, adjust=False, min_periods=20).mean() - 1, rsi / 100,
                  macd / close, signal / close, (macd - signal) / close, returns.rolling(20).std(ddof=0),
                  group.volume / group.volume.rolling(20).mean().replace(0, np.nan), (close - opening) / opening,
                  (high - pd.concat([opening, close], axis=1).max(axis=1)) / close,
                  (pd.concat([opening, close], axis=1).min(axis=1) - low) / close,
                  ((close - opening).abs() / (high - low).replace(0, np.nan) <= 0.1).astype(float),
                  ((close > opening) & (close.shift() < opening.shift()) & (opening <= close.shift()) & (close >= opening.shift())).astype(float),
                  ((close < opening) & (close.shift() > opening.shift()) & (opening >= close.shift()) & (close <= opening.shift())).astype(float)]
        for name, series in zip(FEATURES, values, strict=True):
            group[name] = series
        group["target_return"] = close.shift(-1) / close - 1
        group["target_timestamp"] = group.timestamp.shift(-1)
        group.loc[group.index[:config["warmup_bars"] - 1], FEATURES] = np.nan
        result.append(group)
    if not result:
        raise ValueError("No historical features")
    return pd.concat(result, ignore_index=True).sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def dataset(bars, config):
    frame = feature_frame(bars, config).dropna(subset=FEATURES + ["target_return", "target_timestamp"])
    if frame.empty or not np.isfinite(frame[FEATURES + ["target_return"]].to_numpy()).all():
        raise ValueError("Insufficient finite features after warmup")
    return frame.reset_index(drop=True)


def bars_hash(bars):
    return hashlib.sha256(bars[COLUMNS].to_csv(index=False, float_format="%.17g", date_format="%Y-%m-%dT%H:%M:%SZ").encode()).hexdigest()
