# Configuration and market-data integration

## Migration from the original scaffold

The inspected baseline commit was `ec284658d8082d151005969748489048314930cf`.
It contained only `.gitignore`, `README.md`, `config/trading.example.json`, and this guide.
There was no model, database connector, execution service, or implemented $50,000 limit.
Capital and position fields were `null`. Version 2 introduces the explicitly requested
$100,000 USD starting balance, $25,000 deployment ceiling, and $5,000 per-asset ceiling.

The former percentage-of-current-equity semantics are intentionally replaced by fixed
**starting-balance** percentages. Schema version 1 is rejected, not silently migrated.

## Runtime configuration

Copy [the example](../config/trading.example.json) to ignored `config/trading.local.json`.
The packaged [trading schema](../api-model/src/api_model/schemas/trading.json) rejects missing,
unknown, null, wrongly typed, non-finite, inconsistent, or unsafe settings. Duplicate JSON
keys, booleans supplied as numbers, live flags, shorts, leverage, and unsupported currencies
are rejected. Coherent stricter budgets are allowed; increasing either ceiling is not.

| Field | Contract |
| --- | --- |
| `schema_version` | `2` |
| `mode`, `live_trading_enabled` | `paper`, `false`; there is no live execution implementation |
| `capital.initial_capital`, `capital.currency` | `100000`, `USD` |
| `capital.max_deployed_pct`, `capital.max_deployed_value` | `25`, `25000`; dollars must equal starting balance × percentage / 100 |
| `position_limits.max_position_pct`, `max_position_value` | `5`, `5000`; same fixed starting-balance denominator |
| `asset_policy.allowed_symbols` | Unique uppercase symbols; default `[]` denies new entries |
| `execution.commission_bps`, `slippage_bps` | Baseline 5 bps each, adverse on both sides; fees rounded upward to cents |
| `execution.max_quote_age_seconds` | 60 seconds; future quotes always fail |
| `execution.reservation_ttl_seconds` | 60 seconds; expired/cancelled IDs cannot reopen an intent |
| `execution.exchange_calendar` | `XNYS`; exchange holidays, early closes, and DST handled by exchange-calendars |
| `profit_protection` | Enabled; inclusive 1.0%–1.5% band; strictly above entry; market sell; strict-band gap policy |

All fills in one symbol aggregate into one weighted-average entry. There is no additional
arbitrary position-count restriction: five fully funded positions exhaust the budget;
smaller positions can permit more assets. Currency/asset-class declarations must come from
a trusted feed and the explicit allowlist; no independent security-master service is provided.

Account state is an atomic, permission-restricted JSON file, serialized by a POSIX lock.
The complete configuration hash is pinned to the account. Restart reuses balances, high-water
marks, reservations, order IDs, and fills. Changing the allowlist or any configuration for an
existing account requires a deliberate migration; never delete state to reset a real audit trail.
The research implementation retains its event history in state and is not designed for
unbounded high-frequency operation. Back up state and export the audit history to private storage.

## Historical bars and database schema

CSV headers and `market_bars` table/view columns are exactly:

```text
symbol,timestamp,open,high,low,close,volume,currency,is_complete,is_adjusted
```

- One daily bar per symbol/session; timestamps are timezone-aware bar-end instants.
- All OHLC prices must be positive, finite, internally consistent, and on the same
  split/dividend-adjustment basis. Volume must be finite and nonnegative.
- `currency` is `USD`; `is_complete` and `is_adjusted` are boolean true or integer 1.
  Database reads select completed, adjusted USD rows; CSV ingestion rejects other rows.
- Adjustment/closure flags are upstream attestations, not independently reconstructed.
  Maintain a survivorship-aware universe and handle delistings, suspensions, and corporate
  actions in the data source. Do not mix raw prices with adjusted historical OHLC.
- Duplicates, missing values, naive/future timestamps, bad OHLC, and stale symbol histories
  are rejected. No implicit forward filling occurs. The next-bar target is the next available
  completed bar; suspension gaps are not magically reconstructed.
- The training source is bounded to 2,000,000 selected rows by default. Exceeding the bound
  fails rather than silently dropping old history; provide an explicitly bounded view.

The optional `ingest` command creates the SQLite table and inserts validated CSV rows in
one transaction. Existing duplicate primary keys cause rollback; history is not overwritten.
It is an explicit local write command, separate from the read-only training pipeline.

Set `MARKET_DATABASE_URL` in the environment, never in committed JSON:

```bash
export MARKET_DATABASE_URL="sqlite:///$(pwd)/data/market.sqlite"
# PostgreSQL URL shape only; obtain actual credentials through your secret manager:
# postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=verify-full
```

SQLite is opened with `mode=ro` and `PRAGMA query_only=ON`. PostgreSQL requires TLS,
sets a read-only transaction and 60-second statement timeout, and should use a dedicated
SELECT-only account. Connection errors omit the DSN/driver exception to avoid leaking secrets.
The pipeline does not discover or connect to any unconfigured company database.

## Learning configuration and commands

[training.json](../api-model/config/training.json) and its
[schema](../api-model/src/api_model/schemas/training.json) define daily horizon-one learning,
60-bar warmup, 70/15/15 chronological timestamp partitions, ridge alpha candidates
0.1/1/10/100, minimum split sizes 120/30/30, and seven-day per-symbol freshness.

```bash
api-model --config config/trading.local.json train --output results/training
api-model forecast --model results/training/RUN_ID/model.json --csv data/historical-bars.csv
```

For reproducible historical experiments, ingestion/training/forecast commands accept an
explicit timezone-aware `--as-of`. CLI paper orders and monitoring use the current clock.
Checksummed JSON models avoid unsafe pickle deserialization. Forecasting is informational;
it does not submit buys. Explicit paper buy intents still pass all portfolio checks.

## Quote snapshot and paper commands

A single buy quote uses this object; monitoring reads an array of these objects covering
**all active symbols**. Prices/highs are unadjusted current-session tradable prices, on the
same current share basis as entry. This illustrative timestamp must be replaced with a fresh
valid exchange-session timestamp before running paper orders:

```json
{
  "symbol": "AAA",
  "price": "108.90",
  "timestamp": "2025-01-06T15:00:00Z",
  "session_high": "110.00",
  "currency": "USD",
  "asset_class": "equity"
}
```

`AAA` is a synthetic example, not a selected investment. Configure your own allowlist.
The feed supplies the high since the regular session open, including trades before the
position was opened. The engine persists the maximum observed/supplied high for that session
and resets it on the next exchange session; a restart must not reset the high.

```bash
api-model --config config/trading.local.json buy --quote data/quote.json \
  --quantity 10 --order-id paper-entry-001 --state results/paper-state.json
api-model --config config/trading.local.json monitor --quotes data/quotes.json \
  --state results/paper-state.json --interval 5 --cycles 0
api-model --config config/trading.local.json status --state results/paper-state.json
```

One monitoring cycle validates the entire snapshot before selling any position. Partial,
invalid, or stale snapshots fail atomically and should page an operator. The process stops
on errors rather than silently treating positions as monitored. Run only during valid regular
sessions under a supervisor; no production feed, deployment daemon, or alert integration is included.

## Scheduled learning activation

1. Arrange an accessible, licensed, curated read-only market source matching the schema.
2. Add the `MARKET_DATABASE_URL` GitHub Actions secret. Never paste its value into issues/logs.
3. Confirm artifact privacy, then set repository variable `ENABLE_AUTOMATED_TRAINING=true`.
4. The weekday 22:15 UTC schedule retrains an expanding-history candidate after US market close.
   Manual dispatch also works on `main`, but fails clearly if the database secret is missing.
5. Review held-out metrics and download the candidate artifact. Each run is candidate-only;
   the workflow never trades, commits weights, changes account state, or deploys a model.

GitHub-hosted runners cannot read a workstation's SQLite file. Use a network-accessible TLS
PostgreSQL view, an appropriately secured private runner, or run the CLI locally on a schedule.
Thirty-day workflow artifact retention is not a durable audit archive: export approved run
artifacts and parameter snapshots to access-controlled, retention-managed storage.
