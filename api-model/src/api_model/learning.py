"""Purged chronological learning and immutable, checksummed JSON parameter archives."""

import fcntl
import hashlib
import json
import os
import platform
from importlib.metadata import version
from pathlib import Path

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.preprocessing import StandardScaler

from api_model.config import canonical, digest, load_json, validate
from api_model.market import FEATURES, bars_hash, dataset, feature_frame, iso, utc, validate_bars


def chronological_splits(frame, config):
    times = sorted(frame.timestamp.unique())
    if len(times) < 10:
        raise ValueError("Insufficient distinct timestamps")
    validation_start = times[int(len(times) * config["train_fraction"])]
    test_start = times[int(len(times) * (config["train_fraction"] + config["validation_fraction"]))]
    splits = {
        "train": frame[(frame.timestamp < validation_start) & (frame.target_timestamp < validation_start)],
        "validation": frame[(frame.timestamp >= validation_start) & (frame.timestamp < test_start) & (frame.target_timestamp < test_start)],
        "test": frame[frame.timestamp >= test_start],
        "development": frame[(frame.timestamp < test_start) & (frame.target_timestamp < test_start)],
    }
    for name in ("train", "validation", "test"):
        if len(splits[name]) < config[f"minimum_{name}_rows"] or set(splits[name].symbol) != set(frame.symbol):
            raise ValueError(f"Insufficient {name} rows or missing symbol in chronological split")
    return splits


def metrics(actual, predicted):
    return {"rows": len(actual), "mae": float(mean_absolute_error(actual, predicted)),
            "rmse": float(np.sqrt(mean_squared_error(actual, predicted))),
            "directional_accuracy": float(np.mean(np.sign(actual) == np.sign(predicted)))}


def _fit(frame, alpha):
    scaler = StandardScaler()
    values = scaler.fit_transform(frame[FEATURES].to_numpy())
    model = Ridge(alpha=alpha, solver="svd").fit(values, frame.target_return.to_numpy())
    return scaler, model


def train(bars, training, trading, output, as_of, source_commit="uncommitted"):
    training, trading = validate(training, "training"), validate(trading, "trading")
    bars = validate_bars(bars, as_of, training["max_data_age_days"])
    splits = chronological_splits(dataset(bars, training), training)
    candidates = []
    for alpha in training["ridge_alphas"]:
        scaler, model = _fit(splits["train"], alpha)
        predicted = model.predict(scaler.transform(splits["validation"][FEATURES].to_numpy()))
        candidates.append({"alpha": alpha, **metrics(splits["validation"].target_return.to_numpy(), predicted)})
    selected = min(candidates, key=lambda item: (item["mae"], item["alpha"]))["alpha"]
    scaler, model = _fit(splits["development"], selected)
    test = splits["test"]
    measured = metrics(test.target_return.to_numpy(), model.predict(scaler.transform(test[FEATURES].to_numpy())))
    baseline = metrics(test.target_return.to_numpy(), np.zeros(len(test)))
    ratio = measured["mae"] / baseline["mae"] if baseline["mae"] > 0 else None
    gate = training["gate"]
    eligible = bool(ratio is not None and ratio <= gate["maximum_test_mae_ratio"] and measured["directional_accuracy"] >= gate["minimum_test_directional_accuracy"])
    identity = {"data_sha256": bars_hash(bars), "training_config_sha256": digest(training), "trading_config_sha256": digest(trading), "created_at": iso(as_of), "source_commit": source_commit}
    run_id = digest(identity)[:24]
    payload = {"model_schema_version": 1, "run_id": run_id, **identity, "status": "candidate",
        "eligible_for_paper_evaluation": eligible, "live_deployment": False,
        "software": {name: version(name) for name in ("numpy", "pandas", "scikit-learn", "SQLAlchemy")},
        "python": platform.python_version(), "training_config": training, "trading_config": trading,
        "source_rows": len(bars), "source_start": iso(bars.timestamp.min()), "source_end": iso(bars.timestamp.max()),
        "symbols": sorted(bars.symbol.unique().tolist()), "features": FEATURES,
        "target": "next_completed_bar_close_return", "selected_alpha": selected, "validation_candidates": candidates,
        "scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
        "coefficients": model.coef_.tolist(), "intercept": float(model.intercept_), "fitted_rows": int(scaler.n_samples_seen_),
        "training_labels_through": iso(splits["development"].target_timestamp.max()),
        "splits": {name: {"rows": len(part), "features_from": iso(part.timestamp.min()), "features_through": iso(part.timestamp.max()), "labels_through": iso(part.target_timestamp.max()), "rows_per_symbol": {str(k): int(v) for k, v in part.groupby("symbol").size().items()}} for name, part in splits.items()},
        "test_metrics": measured, "persistence_baseline_metrics": baseline, "test_mae_ratio_to_baseline": ratio,
        "promotion": "Candidate only; explicit paper evaluation is required. No automatic trading or live promotion."}
    serialized = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    folder = output / run_id
    folder.mkdir(exist_ok=False)
    model_path = folder / "model.json"
    model_path.write_text(serialized)
    checksum = hashlib.sha256(serialized.encode()).hexdigest()
    model_path.with_suffix(".sha256").write_text(checksum + "\n")
    (folder / "model-card.md").write_text(f"# Training candidate\n\n- Run: `{run_id}`\n- Source commit: `{source_commit}`\n- SHA-256: `{checksum}`\n- Eligible for paper evaluation: `{eligible}`\n- Test MAE: `{measured['mae']:.8f}`\n- Persistence MAE: `{baseline['mae']:.8f}`\n\nForecast metrics are not a trading backtest or a profitability claim.\n")
    with (output / "parameter-archive.jsonl").open("a") as archive:
        fcntl.flock(archive, fcntl.LOCK_EX)
        archive.write(canonical({"model_sha256": checksum, **payload}) + "\n")
        archive.flush()
        os.fsync(archive.fileno())
        fcntl.flock(archive, fcntl.LOCK_UN)
    return {"run_id": run_id, "model_path": str(model_path), "model_sha256": checksum, "eligible_for_paper_evaluation": eligible, "test_metrics": measured}


def load_model(path):
    path = Path(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != path.with_suffix(".sha256").read_text().strip():
        raise ValueError("Model parameter checksum mismatch")
    model = load_json(path)
    if model.get("model_schema_version") != 1 or model.get("features") != FEATURES or model.get("live_deployment") is not False:
        raise ValueError("Unsupported or unsafe model archive")
    for field in ("scaler_mean", "scaler_scale", "coefficients"):
        values = np.asarray(model[field], dtype=float)
        if values.shape != (len(FEATURES),) or not np.isfinite(values).all():
            raise ValueError("Invalid learned parameter shape or value")
    if (np.asarray(model["scaler_scale"]) <= 0).any() or not np.isfinite(model["intercept"]):
        raise ValueError("Invalid learned scaler or intercept")
    validate(model["training_config"], "training")
    validate(model["trading_config"], "trading")
    return model


def forecast(bars, model, as_of):
    training = validate(model["training_config"], "training")
    bars = validate_bars(bars, as_of, training["max_data_age_days"])
    if not set(bars.symbol).issubset(set(model["symbols"])):
        raise ValueError("Inference contains an unseen symbol")
    latest = feature_frame(bars, training).groupby("symbol", sort=True).tail(1)
    if latest[FEATURES].isna().any().any() or not np.isfinite(latest[FEATURES].to_numpy()).all():
        raise ValueError("Insufficient current indicator history")
    if any(utc(timestamp) <= utc(model["training_labels_through"]) for timestamp in latest.timestamp):
        raise ValueError("Forecast cannot precede the fitted training-label cutoff")
    predicted = ((latest[FEATURES].to_numpy() - np.asarray(model["scaler_mean"])) / np.asarray(model["scaler_scale"])) @ np.asarray(model["coefficients"]) + model["intercept"]
    if not np.isfinite(predicted).all() or (predicted <= -1).any():
        raise ValueError("Forecast is outside the positive-price domain")
    return [{"symbol": row.symbol, "as_of_bar": iso(row.timestamp), "forecast_return": float(value), "forecast_next_close": float(row.close * (1 + value)), "trend": "up" if value > 0 else "down" if value < 0 else "flat", "run_id": model["run_id"], "order_submitted": False} for row, value in zip(latest.itertuples(index=False), predicted, strict=True)]
