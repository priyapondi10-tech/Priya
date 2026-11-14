"""Causal event replay through the same risk and exit engine; no guessed OHLC path."""

from decimal import Decimal
from pathlib import Path

from api_model.config import digest, money
from api_model.engine import PaperEngine
from api_model.market import utc


def replay(events, trading, training, state_path):
    if Path(state_path).exists():
        raise ValueError("Replay requires new state; existing balances are never reset")
    if not events:
        raise ValueError("Replay requires ordered full quote-snapshot events")
    engine = PaperEngine(trading, state_path)
    initial = money(trading["capital"]["initial_capital"])
    peak, drawdown, equity, previous = initial, Decimal(0), initial, None
    curve = []
    for event in events:
        if set(event) != {"timestamp", "quotes", "buy_intents"}:
            raise ValueError("Invalid replay event contract")
        now = utc(event["timestamp"])
        if previous is not None and now <= previous:
            raise ValueError("Replay events must be strictly chronological")
        previous = now
        if any(utc(quote["timestamp"]) != now for quote in event["quotes"]):
            raise ValueError("Replay quotes must match event timestamps")
        exits = engine.monitor(event["quotes"], now)
        by_symbol = {quote["symbol"]: quote for quote in event["quotes"]}
        exited = {order["symbol"] for order in exits}
        for intent in event["buy_intents"]:
            if set(intent) != {"order_id", "symbol", "quantity", "signal_timestamp", "model_training_labels_through", "forecast_return"}:
                raise ValueError("Invalid replay intent")
            if not utc(intent["model_training_labels_through"]) < utc(intent["signal_timestamp"]) < now:
                raise ValueError("Require prior training, a causal signal, and strictly later execution")
            if money(intent["forecast_return"]) <= money(training["baseline"]["minimum_entry_forecast_return"]) or intent["symbol"] in exited:
                continue
            if intent["symbol"] not in by_symbol:
                raise ValueError("Intent has no execution quote")
            engine.buy(intent["order_id"], by_symbol[intent["symbol"]], intent["quantity"], now)
        snapshot = engine.status()
        equity = money(snapshot["cash"]) + sum((position["quantity"] * money(by_symbol[symbol]["price"]) for symbol, position in snapshot["positions"].items()), Decimal(0))
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        curve.append({"timestamp": event["timestamp"], "equity": str(equity), "exits": len(exits)})
    snapshot = engine.status()
    return {"baseline_version": 1, "paper_only": True, "events": len(events), "starting_balance": str(initial),
            "ending_equity": str(equity), "total_return": str(equity / initial - 1), "maximum_drawdown": str(drawdown),
            "trading_config_sha256": digest(trading), "training_config_sha256": digest(training),
            "commission_bps": trading["execution"]["commission_bps"], "slippage_bps": trading["execution"]["slippage_bps"],
            "positions_at_end": snapshot["positions"], "fills": snapshot["fills"], "equity_curve": curve,
            "limitations": "Caller must supply causal signals and full-session highs. No daily-OHLC intrabar sequence is inferred. Open positions are marked, not forcibly sold. No profitability claim."}
