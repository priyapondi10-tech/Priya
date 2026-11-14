import json
from datetime import timedelta
from decimal import Decimal

import pytest
from api_model.backtest import replay
from api_model.cli import main


def events(now, quote):
    intent = {"order_id": "entry", "symbol": "AAA", "quantity": 50,
              "signal_timestamp": "2025-01-03T21:00:00Z", "model_training_labels_through": "2025-01-02T21:00:00Z", "forecast_return": 0.003}
    return [{"timestamp": now.isoformat(), "quotes": [quote()], "buy_intents": [intent]},
            {"timestamp": (now + timedelta(minutes=1)).isoformat(), "quotes": [quote(price="110", at=now + timedelta(minutes=1))], "buy_intents": []},
            {"timestamp": (now + timedelta(minutes=2)).isoformat(), "quotes": [quote(price="108.9", high="110", at=now + timedelta(minutes=2))], "buy_intents": []}]


def test_replay(config, training, now, quote, tmp_path):
    path = tmp_path / "state.json"
    report = replay(events(now, quote), config, training, path)
    assert Decimal(report["ending_equity"]) == 100445
    assert len(report["fills"]) == 2 and not report["positions_at_end"]
    assert report["fills"][-1]["side"] == "sell"
    with pytest.raises(ValueError, match="never reset"):
        replay(events(now, quote), config, training, path)


@pytest.mark.parametrize("field", ["signal_timestamp", "model_training_labels_through"])
def test_no_replay_lookahead(config, training, now, quote, tmp_path, field):
    data = events(now, quote)
    data[0]["buy_intents"][0][field] = now.isoformat()
    with pytest.raises(ValueError, match="prior training"):
        replay(data, config, training, tmp_path / "state.json")


def test_baseline_entry_threshold(config, training, now, quote, tmp_path):
    data = events(now, quote)
    data[0]["buy_intents"][0]["forecast_return"] = 0.001
    assert not replay(data, config, training, tmp_path / "state.json")["fills"]


def test_cli_validation_and_missing_database(monkeypatch, capsys):
    from conftest import ROOT
    monkeypatch.chdir(ROOT)
    assert main(["validate-config"]) == 0
    assert json.loads(capsys.readouterr().out)["deployment_limit"] == 25000
    monkeypatch.delenv("MARKET_DATABASE_URL", raising=False)
    assert main(["train"]) == 1
    assert "MARKET_DATABASE_URL" in capsys.readouterr().err


def test_cli_replay(config, now, quote, tmp_path, monkeypatch, capsys):
    from conftest import ROOT
    monkeypatch.chdir(ROOT)
    cfg, data, report = (tmp_path / name for name in ("config.json", "events.json", "report.json"))
    cfg.write_text(json.dumps(config))
    data.write_text(json.dumps(events(now, quote)))
    assert main(["--config", str(cfg), "replay", "--events", str(data), "--state", str(tmp_path / "state.json"), "--output", str(report)]) == 0
    assert json.loads(report.read_text())["events_sha256"]
    capsys.readouterr()
