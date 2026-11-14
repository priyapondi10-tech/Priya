"""Explicit local commands; scheduled jobs only train and archive candidates."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

from api_model.backtest import replay
from api_model.config import digest, load_config, load_json
from api_model.engine import PaperEngine, current_time
from api_model.learning import forecast, load_model, train
from api_model.market import ingest_csv, read_database, utc


def parser():
    result = argparse.ArgumentParser(prog="api-model")
    result.add_argument("--config", type=Path, default=Path("config/trading.example.json"))
    result.add_argument("--training-config", type=Path, default=Path("api-model/config/training.json"))
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-config")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("--csv", type=Path, required=True)
    ingest.add_argument("--sqlite", type=Path, required=True)
    ingest.add_argument("--as-of")
    learn = commands.add_parser("train")
    learn.add_argument("--output", type=Path, default=Path("results/training"))
    learn.add_argument("--as-of")
    learn.add_argument("--source-commit", default=os.environ.get("GITHUB_SHA", "uncommitted"))
    predict = commands.add_parser("forecast")
    predict.add_argument("--model", type=Path, required=True)
    predict.add_argument("--csv", type=Path, required=True)
    predict.add_argument("--as-of")
    for name in ("status", "buy", "monitor"):
        command = commands.add_parser(name)
        command.add_argument("--state", type=Path, default=Path("results/paper-state.json"))
        if name == "buy":
            command.add_argument("--quote", type=Path, required=True)
            command.add_argument("--quantity", type=int, required=True)
            command.add_argument("--order-id", required=True)
        if name == "monitor":
            command.add_argument("--quotes", type=Path, required=True)
            command.add_argument("--interval", type=float, default=5)
            command.add_argument("--cycles", type=int, default=1, help="0 means continuous")
    backtest = commands.add_parser("replay")
    backtest.add_argument("--events", type=Path, required=True)
    backtest.add_argument("--state", type=Path, required=True)
    backtest.add_argument("--output", type=Path, required=True)
    return result


def run(args):
    trading, training = load_config(args.config), load_config(args.training_config, "training")
    if args.command == "validate-config":
        return {"valid": True, "paper_only": True, "trading_config_sha256": digest(trading),
                "deployment_limit": trading["capital"]["max_deployed_value"], "position_limit": trading["position_limits"]["max_position_value"]}
    if args.command == "ingest":
        return {"rows_ingested": ingest_csv(args.csv, args.sqlite, utc(args.as_of) if args.as_of else current_time())}
    if args.command == "train":
        url = os.environ.get("MARKET_DATABASE_URL")
        if not url:
            raise ValueError("Set MARKET_DATABASE_URL to a read-only database; no data source is assumed")
        as_of = utc(args.as_of) if args.as_of else current_time()
        return train(read_database(url, training, as_of), training, trading, args.output, as_of, args.source_commit)
    if args.command == "forecast":
        model = load_model(args.model)
        return {"forecasts": forecast(pd.read_csv(args.csv), model, utc(args.as_of) if args.as_of else current_time()),
                "candidate_only": True, "eligible_for_paper_evaluation": model["eligible_for_paper_evaluation"]}
    if args.command == "replay":
        if args.output.exists():
            raise ValueError("Replay report already exists; overwrite is forbidden")
        report = replay(load_json(args.events), trading, training, args.state)
        report["events_sha256"] = hashlib.sha256(args.events.read_bytes()).hexdigest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
        return report
    engine = PaperEngine(trading, args.state)
    if args.command == "status":
        return {"paper_only": True, "currency": "USD", **engine.status()}
    if args.command == "buy":
        return engine.buy(args.order_id, load_json(args.quote), args.quantity, current_time())
    if args.command == "monitor":
        if args.interval <= 0 or args.cycles < 0:
            raise ValueError("Require a positive interval and nonnegative cycles")
        completed = 0
        while True:
            orders = engine.monitor(load_json(args.quotes), current_time())
            completed += 1
            print(json.dumps({"cycle": completed, "orders": orders, "paper_only": True}), flush=True)
            if args.cycles and completed >= args.cycles:
                return {"cycles_completed": completed, "paper_only": True}
            time.sleep(args.interval)
    raise ValueError("Unknown command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        print(json.dumps(run(args), indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "paper_only": True}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
