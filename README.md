# Priya: api-model trading research engine

Runnable historical learning, strict portfolio risk controls, and **paper-only** execution.
The original four-file configuration scaffold has been extended under `api-model/`.
There is no broker adapter or live-trading order path. No market database or credentials
are assumed, and no real-data performance is claimed.

## Policy defaults

| Control | Enforced value |
| --- | --- |
| Base currency / starting balance | USD / $100,000 |
| Maximum deployed budget | 25% of starting balance; $25,000 |
| Maximum aggregate allocation per asset | 5% of starting balance; $5,000 |
| Eligible instruments | Explicitly allowlisted USD equities; whole shares; long-only |
| Borrowing, leverage, live execution | Disabled and rejected by configuration validation |
| Profit protection | Sell the entire position at market in the paper engine when the daily-high pullback is inclusively 1.0%–1.5% and price is strictly above average entry |

Budgets include entry commissions and adverse slippage. Gains count at market value;
losses do not create additional deployment headroom. Both limits use **starting balance**,
not current equity. An empty allowlist intentionally blocks all new entries.

## Install and verify

Use Python 3.11–3.13 on Linux/macOS. Run commands from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e './api-model[dev]'
api-model validate-config
python scripts/validate_repository.py
ruff check --config api-model/pyproject.toml api-model scripts
python -m pytest -c api-model/pyproject.toml api-model/tests
```

## Configure and learn

```bash
cp config/trading.example.json config/trading.local.json
# Edit the local allowed_symbols list before creating a paper account.
api-model --config config/trading.local.json validate-config

# Optional: import your own completed, consistently adjusted daily CSV bars.
api-model ingest --csv data/historical-bars.csv --sqlite data/market.sqlite
export MARKET_DATABASE_URL="sqlite:///$(pwd)/data/market.sqlite"
api-model --config config/trading.local.json train --output results/training
```

The CSV/database contract and PostgreSQL configuration are in the
[configuration guide](docs/configuration.md). Training uses SMA(20), EMA(20), Wilder RSI(14),
MACD(12,26,9), price/volume features, and candle-body/wick/doji/engulfing patterns.
Chronological splits, purged forward labels, held-out metrics, and full JSON coefficient
archives support reproducible evaluation. Training never submits orders or promotes live models.

The [learning workflow](.github/workflows/continuous-learning.yml) is scheduled for
22:15 UTC on weekdays. To activate it, configure the `MARKET_DATABASE_URL` Actions secret
and set the `ENABLE_AUTOMATED_TRAINING` repository variable to `true`. A GitHub-hosted
runner needs an accessible TLS PostgreSQL source; local SQLite requires a suitable private
runner or local scheduling. Training is **not active until the data source is configured**.

## Monitor a paper portfolio

Create explicit paper buy intents with `api-model buy`; then run:

```bash
api-model --config config/trading.local.json monitor \
  --state results/paper-state.json --quotes data/quotes.json --interval 5 --cycles 0
```

Your feed adapter must atomically refresh a full snapshot covering every active position,
including each asset's full regular-session daily high. Missing, stale, future, or
out-of-session snapshots are rejected; supervision and alerting remain operator tasks.
**A first observed pullback beyond 1.5% does not trigger this exact-band rule.** This is
not a stop-loss and cannot guarantee net profit after fees/slippage or a gap.

Read the [strategy archive and audit guide](docs/strategy-archive.md) for decision formulas,
backtest event contracts, learned-parameter records, and operational limitations.

## Public repository and private data

Never commit market databases, raw private bars, model artifacts, account state, credentials,
or broker identifiers. These outputs belong in ignored local/private storage. Public-repository
Actions artifacts can expose parameters and symbol universes; approve data licensing and
artifact access before enabling automated training. Ignore rules are not a security guarantee.
