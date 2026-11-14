"""Locked, atomic paper portfolio with fixed-budget risk and daily-high exits.

POSIX file locking serializes workers; no broker or live network order path exists.
"""

import copy
import fcntl
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from functools import lru_cache
from pathlib import Path

import exchange_calendars as calendars
import pandas as pd

from api_model.config import atomic_json, digest, load_json, money, validate
from api_model.market import iso, utc


@lru_cache(maxsize=8)
def calendar(year):
    return calendars.get_calendar("XNYS", start=f"{year - 1}-01-01", end=f"{year + 1}-12-31")


def should_protect(entry, price, high, rule):
    entry, price, high = money(entry), money(price), money(high)
    if min(entry, price, high) <= 0 or price > high:
        raise ValueError("Invalid entry, price, or daily high")
    numerator = (high - price) * 100
    return bool(price > entry and high * money(rule["pullback_min_pct"]) <= numerator <= high * money(rule["pullback_max_pct"]))


def current_time():
    return datetime.now(timezone.utc)


class PaperEngine:
    def __init__(self, config, path):
        self.config = validate(config, "trading")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if not self.path.exists():
                atomic_json(self.path, {"state_version": 1, "config_sha256": digest(self.config),
                    "starting_balance": str(self.config["capital"]["initial_capital"]), "cash": str(self.config["capital"]["initial_capital"]),
                    "positions": {}, "quotes": {}, "pending_buys": {}, "orders": {}, "fills": [], "events": []})
            self._read()

    @contextmanager
    def _lock(self):
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        state = load_json(self.path)
        if state.get("state_version") != 1 or state.get("config_sha256") != digest(self.config):
            raise ValueError("Portfolio configuration changed; explicit migration is required, not a balance reset")
        if money(state["starting_balance"]) != money(self.config["capital"]["initial_capital"]) or money(state["cash"]) < 0:
            raise ValueError("Invalid account state")
        return state

    @staticmethod
    def _event(state, now, kind, details):
        state["events"].append({"timestamp": iso(now), "type": kind, "details": details})

    @contextmanager
    def _transaction(self, action, now):
        now = utc(now)
        with self._lock():
            state = self._read()
            for order_id, order in list(state["pending_buys"].items()):
                if utc(order["expires"]) <= now:
                    del state["pending_buys"][order_id]
                    state["orders"][order_id]["status"] = "expired"
                    self._event(state, now, "reservation_expired", {"order_id": order_id})
            original = copy.deepcopy(state)
            try:
                yield state
            except Exception as exc:
                self._event(original, now, "rejected", {"action": action, "reason": str(exc)})
                atomic_json(self.path, original)
                raise
            else:
                atomic_json(self.path, state)

    def _quote(self, state, quote, now):
        if set(quote) != {"symbol", "price", "timestamp", "session_high", "currency", "asset_class"}:
            raise ValueError("Invalid quote snapshot contract")
        symbol = quote["symbol"]
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol):
            raise ValueError("Invalid quote symbol")
        price, high, timestamp = money(quote["price"]), money(quote["session_high"]), utc(quote["timestamp"])
        if quote["currency"] != "USD" or quote["asset_class"] != "equity" or price <= 0 or high < price:
            raise ValueError("Require positive USD equity prices and a full-session high >= price")
        if not 0 <= (utc(now) - timestamp).total_seconds() <= self.config["execution"]["max_quote_age_seconds"]:
            raise ValueError("Quote is stale or from the future")
        minute = pd.Timestamp(timestamp).floor("min")
        exchange = calendar(minute.year)
        if not exchange.is_open_on_minute(minute):
            raise ValueError("Execution requires an open XNYS regular session, including holidays and early closes")
        session = str(exchange.minute_to_session(minute).date())
        previous = state["quotes"].get(symbol)
        if previous:
            if timestamp < utc(previous["timestamp"]):
                raise ValueError("Out-of-order quote")
            if timestamp == utc(previous["timestamp"]) and price != money(previous["price"]):
                raise ValueError("Conflicting quote timestamp")
            if previous["session"] == session:
                high = max(high, money(previous["high"]))
        state["quotes"][symbol] = {"price": str(price), "high": str(high), "timestamp": iso(timestamp), "session": session}
        return symbol, price, high

    def _price(self, value, side):
        slip = money(self.config["execution"]["slippage_bps"]) / 10000
        return (money(value) * (1 + slip if side == "buy" else 1 - slip)).quantize(Decimal("0.0001"), rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR)

    def _fee(self, notional):
        return (notional * money(self.config["execution"]["commission_bps"]) / 10000).quantize(Decimal("0.01"), rounding=ROUND_CEILING)

    def _risk(self, state, symbol, quantity, now, exclude=None):
        if symbol not in self.config["asset_policy"]["allowed_symbols"]:
            raise ValueError("Symbol not allowlisted; an empty allowlist blocks all new entries")
        pending = {key: value for key, value in state["pending_buys"].items() if key != exclude}
        needed = {symbol} | set(state["positions"]) | {value["symbol"] for value in pending.values()}
        for asset in needed:
            quote = state["quotes"].get(asset)
            if not quote or not 0 <= (utc(now) - utc(quote["timestamp"])).total_seconds() <= self.config["execution"]["max_quote_age_seconds"]:
                raise ValueError("Fresh marks required for every held and reserved asset")
        exposure = {asset: position["quantity"] * max(money(position["entry"]), money(state["quotes"][asset]["price"])) + money(position["entry_fees"]) for asset, position in state["positions"].items()}
        reserved_cash = Decimal(0)
        for order in pending.values():
            asset = order["symbol"]
            notional = max(money(order["notional"]), order["quantity"] * self._price(state["quotes"][asset]["price"], "buy"))
            exposure[asset] = exposure.get(asset, Decimal(0)) + notional + self._fee(notional)
            reserved_cash += notional + self._fee(notional)
        price = self._price(state["quotes"][symbol]["price"], "buy")
        notional, fee = quantity * price, self._fee(quantity * price)
        exposure[symbol] = exposure.get(symbol, Decimal(0)) + notional + fee
        if any(value > money(self.config["position_limits"]["max_position_value"]) for value in exposure.values()):
            raise ValueError("Aggregate per-asset exposure exceeds the 5%/$5,000 starting-balance ceiling")
        if sum(exposure.values(), Decimal(0)) > money(self.config["capital"]["max_deployed_value"]):
            raise ValueError("Portfolio exposure exceeds the 25%/$25,000 starting-balance ceiling")
        if reserved_cash + notional + fee > money(state["cash"]):
            raise ValueError("Insufficient unreserved cash including fees and slippage")
        return price, notional, fee

    def _fill(self, state, order_id, symbol, quantity, now):
        price, notional, fee = self._risk(state, symbol, quantity, now, exclude=order_id)
        old = state["positions"].get(symbol, {"quantity": 0, "entry": "0", "entry_fees": "0", "origin": order_id})
        total = old["quantity"] + quantity
        state["positions"][symbol] = {"quantity": total, "entry": str((old["quantity"] * money(old["entry"]) + notional) / total), "entry_fees": str(money(old["entry_fees"]) + fee), "origin": old["origin"]}
        state["cash"] = str(money(state["cash"]) - notional - fee)
        state["pending_buys"].pop(order_id, None)
        state["orders"][order_id] = {"side": "buy", "symbol": symbol, "quantity": quantity, "status": "filled"}
        result = {"order_id": order_id, "side": "buy", "order_type": "market", "symbol": symbol, "quantity": quantity, "price": str(price), "fees": str(fee), "timestamp": iso(now), "status": "filled", "paper_only": True}
        state["fills"].append(result)
        self._event(state, now, "paper_market_buy", result)
        return result

    def buy(self, order_id, quote, quantity, now, reserve_only=False):
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", order_id) or type(quantity) is not int or quantity <= 0:
            raise ValueError("Require a valid order ID and positive whole-share quantity")
        with self._transaction("buy", now) as state:
            old = state["orders"].get(order_id)
            if old:
                if old["side"] != "buy" or old["symbol"] != quote["symbol"] or old["quantity"] != quantity:
                    raise ValueError("Order ID already used for a different intent")
                return {"order_id": order_id, "status": old["status"], "duplicate": True}
            symbol, _, _ = self._quote(state, quote, now)
            _, notional, fee = self._risk(state, symbol, quantity, now)
            if reserve_only:
                state["pending_buys"][order_id] = {"symbol": symbol, "quantity": quantity, "notional": str(notional), "fees": str(fee), "expires": iso(utc(now) + timedelta(seconds=self.config["execution"]["reservation_ttl_seconds"]))}
                state["orders"][order_id] = {"side": "buy", "symbol": symbol, "quantity": quantity, "status": "reserved"}
                self._event(state, now, "buy_reserved", {"order_id": order_id, **state["pending_buys"][order_id]})
                return {"order_id": order_id, "status": "reserved", "paper_only": True}
            return self._fill(state, order_id, symbol, quantity, now)

    def fill_reserved(self, order_id, quote, now):
        with self._transaction("fill_reserved", now) as state:
            old = state["orders"].get(order_id)
            if not old or old["side"] != "buy" or old["symbol"] != quote["symbol"]:
                raise ValueError("Unknown or mismatched reservation")
            if old["status"] != "reserved":
                return {"order_id": order_id, "status": old["status"], "duplicate": True}
            symbol, _, _ = self._quote(state, quote, now)
            return self._fill(state, order_id, symbol, old["quantity"], now)

    def cancel_reserved(self, order_id, now):
        with self._transaction("cancel_reserved", now) as state:
            if state["pending_buys"].pop(order_id, None):
                state["orders"][order_id]["status"] = "cancelled"
                self._event(state, now, "buy_cancelled", {"order_id": order_id})

    def _sell(self, state, symbol, price, high, now):
        position = state["positions"].pop(symbol)
        executed = self._price(price, "sell")
        notional, fee = executed * position["quantity"], self._fee(executed * position["quantity"])
        net = notional - fee - position["quantity"] * money(position["entry"]) - money(position["entry_fees"])
        state["cash"] = str(money(state["cash"]) + notional - fee)
        for order_id, order in list(state["pending_buys"].items()):
            if order["symbol"] == symbol:
                del state["pending_buys"][order_id]
                state["orders"][order_id]["status"] = "cancelled"
                self._event(state, now, "buy_cancelled_on_exit", {"order_id": order_id})
        order_id = "protect:" + digest({"symbol": symbol, "origin": position["origin"]})[:32]
        result = {"order_id": order_id, "side": "sell", "order_type": "market", "symbol": symbol, "quantity": position["quantity"], "price": str(executed), "fees": str(fee), "realized_pnl": str(net), "daily_high": str(high), "trigger_price": str(price), "timestamp": iso(now), "status": "filled", "reason": "daily_high_profit_protection", "paper_only": True}
        state["orders"][order_id] = {"side": "sell", "symbol": symbol, "quantity": position["quantity"], "status": "filled"}
        state["fills"].append(result)
        self._event(state, now, "profit_protection_market_sell", result)
        return result

    def monitor(self, quotes, now):
        with self._transaction("monitor", now) as state:
            symbols = [quote["symbol"] for quote in quotes]
            if len(set(symbols)) != len(symbols) or set(state["positions"]) - set(symbols):
                raise ValueError("Snapshot has duplicates or is missing active positions; no exits processed")
            values = {symbol: (price, high) for symbol, price, high in (self._quote(state, quote, now) for quote in quotes)}
            exits = []
            count = len(state["positions"])
            for symbol, position in list(state["positions"].items()):
                price, high = values[symbol]
                if should_protect(position["entry"], price, high, self.config["profit_protection"]):
                    exits.append(self._sell(state, symbol, price, high, now))
            self._event(state, now, "monitor_snapshot", {"positions_checked": count, "exits": len(exits)})
            return exits

    def status(self):
        with self._lock():
            return self._read()
