from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from api_model.engine import PaperEngine, should_protect


@pytest.mark.parametrize("price,expected", [("108.911", False), ("108.900", True), ("108.350", True), ("108.339", False), ("110", False), ("100", False), ("99", False)])
def test_profit_band(config, price, expected):
    assert should_protect("100", price, "110", config["profit_protection"]) is expected


def test_above_entry_is_strict(config):
    assert not should_protect("108.9", "108.9", "110", config["profit_protection"])


def test_per_asset_aggregates_all_fills(config, quote, now, tmp_path):
    engine = PaperEngine(config, tmp_path / "state.json")
    engine.buy("one", quote(), 30, now)
    engine.buy("two", quote(), 20, now)
    with pytest.raises(ValueError, match="per-asset"):
        engine.buy("excess", quote(), 1, now)
    state = engine.status()
    assert state["positions"]["AAA"]["quantity"] == 50 and Decimal(state["cash"]) == 95000


def test_total_cap(config, quote, now, tmp_path):
    engine = PaperEngine(config, tmp_path / "state.json")
    for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        engine.buy(symbol, quote(symbol=symbol), 50, now)
    with pytest.raises(ValueError, match="Portfolio exposure"):
        engine.buy("excess", quote(symbol="FFF"), 1, now)
    assert Decimal(engine.status()["cash"]) == 75000


def test_pending_orders_count_and_fill_rechecks(config, quote, now, tmp_path):
    engine = PaperEngine(config, tmp_path / "state.json")
    engine.buy("reserved", quote(), 50, now, reserve_only=True)
    with pytest.raises(ValueError, match="per-asset"):
        engine.buy("more", quote(), 1, now)
    later = now + timedelta(seconds=1)
    with pytest.raises(ValueError, match="per-asset"):
        engine.fill_reserved("reserved", quote(price="101", at=later), later)
    assert not engine.status()["positions"]
    engine.fill_reserved("reserved", quote(at=later), later)
    assert engine.fill_reserved("reserved", quote(at=later), later)["duplicate"]


def test_slippage_and_fees(raw, quote, now, tmp_path):
    raw["asset_policy"]["allowed_symbols"] = ["AAA"]
    engine = PaperEngine(raw, tmp_path / "state.json")
    with pytest.raises(ValueError, match="per-asset"):
        engine.buy("too-large", quote(), 50, now)
    filled = engine.buy("allowed", quote(), 49, now)
    assert Decimal(filled["price"]) == Decimal("100.05")
    assert Decimal(filled["fees"]) == Decimal("2.46")
    assert Decimal(engine.status()["cash"]) == Decimal("95095.09")


@pytest.mark.parametrize("new_price", ["90", "101"])
def test_losses_do_not_free_budget_and_gains_count(config, quote, now, tmp_path, new_price):
    engine = PaperEngine(config, tmp_path / "state.json")
    engine.buy("entry", quote(), 50, now)
    later = now + timedelta(seconds=1)
    engine.monitor([quote(price=new_price, at=later)], later)
    with pytest.raises(ValueError, match="per-asset"):
        engine.buy("add", quote(price=new_price, at=later), 1, later)


def test_all_positions_need_fresh_marks(config, quote, now, tmp_path):
    engine = PaperEngine(config, tmp_path / "state.json")
    engine.buy("entry", quote(), 10, now)
    later = now + timedelta(seconds=61)
    with pytest.raises(ValueError, match="Fresh marks"):
        engine.buy("other", quote(symbol="BBB", at=later), 1, later)


def test_idempotence_and_restart(config, quote, now, tmp_path):
    path = tmp_path / "state.json"
    PaperEngine(config, path).buy("same", quote(), 10, now)
    engine = PaperEngine(config, path)
    assert engine.buy("same", quote(), 10, now)["duplicate"]
    with pytest.raises(ValueError, match="different intent"):
        engine.buy("same", quote(), 11, now)
    assert len(engine.status()["fills"]) == 1


def test_expired_and_cancelled_ids_never_reopen(config, quote, now, tmp_path):
    engine = PaperEngine(config, tmp_path / "state.json")
    engine.buy("expired", quote(), 10, now, reserve_only=True)
    later = now + timedelta(seconds=61, microseconds=1)
    assert engine.buy("expired", quote(at=later), 10, later)["status"] == "expired"
    engine.buy("cancelled", quote(at=later), 10, later, reserve_only=True)
    engine.cancel_reserved("cancelled", later)
    assert engine.buy("cancelled", quote(at=later), 10, later)["status"] == "cancelled"
    assert not engine.status()["positions"]


def test_high_persistence_exit_and_pending_cancellation(config, quote, now, tmp_path):
    path = tmp_path / "state.json"
    engine = PaperEngine(config, path)
    engine.buy("entry", quote(), 10, now)
    engine.buy("reserved", quote(), 10, now, reserve_only=True)
    later = now + timedelta(seconds=1)
    assert engine.monitor([quote(price="110", at=later)], later) == []
    later += timedelta(seconds=1)
    engine = PaperEngine(config, path)
    orders = engine.monitor([quote(price="108.9", high="109", at=later)], later)
    assert orders[0]["side"] == "sell" and Decimal(orders[0]["daily_high"]) == 110
    assert not engine.status()["pending_buys"]
    assert not engine.status()["positions"]
    assert engine.buy("reserved", quote(at=later), 10, later)["status"] == "cancelled"
    assert engine.monitor([quote(price="108.9", high="110", at=later)], later) == []


def test_new_day_high_resets(config, quote, now, tmp_path):
    engine = PaperEngine(config, tmp_path / "state.json")
    engine.buy("entry", quote(), 10, now)
    later = now + timedelta(seconds=1)
    engine.monitor([quote(price="110", at=later)], later)
    next_day = now + timedelta(days=1)
    assert engine.monitor([quote(price="108.9", at=next_day)], next_day) == []
    assert engine.status()["positions"]["AAA"]["quantity"] == 10


def test_every_active_position_and_atomic_snapshot(config, quote, now, tmp_path):
    engine = PaperEngine(config, tmp_path / "state.json")
    engine.buy("one", quote(), 10, now)
    engine.buy("two", quote(symbol="BBB"), 10, now)
    later = now + timedelta(seconds=1)
    a, b = quote(price="108.9", high="110", at=later), quote(symbol="BBB", price="108.35", high="110", at=later)
    with pytest.raises(ValueError, match="missing active"):
        engine.monitor([a], later)
    assert len(engine.status()["positions"]) == 2
    assert len(engine.monitor([a, b], later)) == 2
    assert not engine.status()["positions"]
    assert any(event["type"] == "rejected" for event in engine.status()["events"])


@pytest.mark.parametrize("age", [-1, 61])
def test_stale_or_future(config, quote, now, tmp_path, age):
    engine = PaperEngine(config, tmp_path / "state.json")
    with pytest.raises(ValueError, match="stale or from the future"):
        engine.buy("bad", quote(at=now - timedelta(seconds=age)), 1, now)


@pytest.mark.parametrize("at", ["2025-01-04T15:00:00Z", "2025-01-20T15:00:00Z", "2025-11-28T19:00:00Z"])
def test_market_calendar(config, quote, tmp_path, at):
    now = datetime.fromisoformat(at.replace("Z", "+00:00"))
    engine = PaperEngine(config, tmp_path / "state.json")
    with pytest.raises(ValueError, match="open XNYS"):
        engine.buy("closed", quote(at=now), 1, now)


def test_allowlist_and_config_migration(raw, config, quote, now, tmp_path):
    with pytest.raises(ValueError, match="allowlisted"):
        PaperEngine(raw, tmp_path / "empty.json").buy("entry", quote(), 1, now)
    path = tmp_path / "state.json"
    PaperEngine(config, path).buy("entry", quote(), 10, now)
    with pytest.raises(ValueError, match="migration"):
        PaperEngine(raw, path)
    assert Decimal(PaperEngine(config, path).status()["cash"]) == 99000


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True])
def test_whole_positive_shares_only(config, quote, now, tmp_path, quantity):
    with pytest.raises(ValueError, match="whole-share"):
        PaperEngine(config, tmp_path / "state.json").buy("entry", quote(), quantity, now)


def test_concurrent_reservations(config, quote, now, tmp_path):
    engine = PaperEngine(config, tmp_path / "state.json")
    def reserve(order_id):
        try:
            return engine.buy(order_id, quote(), 30, now, reserve_only=True)["status"]
        except ValueError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, ["one", "two"]))
    assert sorted(outcomes) == ["rejected", "reserved"]
    assert len(engine.status()["pending_buys"]) == 1


def test_fees_cannot_escape_deployment_budget(raw, quote, now, tmp_path):
    raw["asset_policy"]["allowed_symbols"] = ["AAA"]
    raw["execution"].update(commission_bps=100, slippage_bps=0)
    engine = PaperEngine(raw, tmp_path / "state.json")
    with pytest.raises(ValueError, match="per-asset"):
        engine.buy("fees-over-cap", quote(), 50, now)


@pytest.mark.parametrize("field,value", [("currency", "EUR"), ("asset_class", "option"), ("price", "NaN"), ("session_high", "99")])
def test_invalid_quotes(config, quote, now, tmp_path, field, value):
    tick = quote()
    tick[field] = value
    with pytest.raises(ValueError):
        PaperEngine(config, tmp_path / "state.json").buy("invalid", tick, 1, now)
