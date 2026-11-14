# Strategy archive and audit guide

## Scope and initial archive entry

This guide records the implemented paper strategy, risk policy, learning protocol, and
parameter-archive contract. The inspected source baseline was
`ec284658d8082d151005969748489048314930cf`: four configuration/documentation files, no
trained model, database connector, broker, or executable trading strategy. There was no
implemented $50,000 ceiling; capital settings were unset. This implementation establishes
the requested USD $100,000 account, $25,000 total ceiling, and $5,000 per-asset ceiling.

| Archive entry | Recorded state |
| --- | --- |
| Policy initialization | 2026-11-14; trading schema 2; learning schema 1 |
| Actual company-market-data training runs | None supplied or executed during implementation |
| Learned coefficients/scaler/metrics | Not yet available; never fabricated from example values |
| Default eligible symbols | Empty allowlist; operator must select authorized equities |
| Deployment status | Paper-only; no broker adapter or live order path |
| Baseline backtest results | Not established on real market data; unit fixtures are synthetic correctness checks |

Future learned values are written by the pipeline, not manually invented in this guide.
Keep proprietary parameter archives in approved private storage, not this public repository.

## Decision logic

1. Load and validate explicit configuration. Reject missing/unknown fields, duplicate JSON
   keys, nulls, non-finite numbers, incompatible schema versions, and unsafe trading flags.
2. Load the existing locked account state. Verify its configuration hash; never reset
   balances, open positions, order IDs, or daily highs on restart.
3. In a monitoring cycle, require a fresh, valid, complete snapshot for every active asset.
   Validate the whole snapshot before processing any exit; errors produce a rejected audit
   event and no partial exits.
4. Update each symbol's regular-session high and evaluate profit protection independently
   for every open position. A triggered exit liquidates the full aggregate position using
   a simulated market sell, adverse slippage, and commissions.
5. New exposure requires an explicit paper buy intent. Forecast output alone does not buy.
   Aggregate existing fills, pending reservations, and the proposal; validate both budgets,
   fresh portfolio marks, allowlist, whole shares, currency, and available cash.
6. Reserve or fill atomically. Recheck all limits at reservation fill time. Duplicate accepted
   order IDs never create another fill; cancelled/expired IDs never reopen an old intent.

The event-replay baseline additionally requires forecast return strictly greater than 0.002
(20 bps), a signal after the model's training-label cutoff, and execution strictly after the
signal. It skips same-event re-entry after a protective exit. These are explicit research
baseline assumptions, not a claim that an entry rule is profitable.

## Indicators and forecast model

Features are calculated independently for each symbol using current and earlier completed,
consistently adjusted daily bars only. No cross-symbol rolling windows or forward fills occur.

| Feature group | Definition |
| --- | --- |
| Returns | One-bar and five-bar close returns |
| SMA(20) | Current close / trailing 20-close arithmetic mean − 1 |
| EMA(20) | Current close / recursive EMA(20) − 1; alpha = 2/21 |
| Wilder RSI(14) | Fourteen-change arithmetic seed, then Wilder recursion; scaled to 0–1; flat = 0.5, all-gain = 1, all-loss = 0 |
| MACD(12,26,9) | EMA(12) − EMA(26), its EMA(9) signal, and histogram; each normalized by current close |
| Volatility and volume | Twenty-return population standard deviation; volume / trailing 20-bar mean volume |
| Candle shape | Signed body / open; upper and lower wicks / close |
| Candle patterns | Doji if absolute body / range ≤ 10%; bullish and bearish body-engulfing flags against the prior candle |

The first 59 rows per symbol are excluded by the 60-bar warmup. Zero-volume windows may
produce unavailable features and are not silently imputed. The target is
`next_available_completed_close / current_close - 1`; the final unlabeled bar is excluded
from training. Suspension gaps and missing exchange sessions require upstream data review.

The model is standardized ridge regression with an intercept. A prediction is a one-bar
return forecast and corresponding next-close estimate, not a calibrated probability.
Inference rejects unsupported feature layouts, unseen symbols, invalid parameters,
insufficient history, nonpositive implied prices, or an inference bar at/before the fitted
training-label cutoff. Model archives use JSON, never executable pickle files.

## Automated learning and evaluation

The weekday 22:15 UTC workflow performs a full cumulative-history retrain, not an unbounded
online update after every tick. Database reads select completed, adjusted USD rows from the
configured `market_bars` table/view. SQLite is read-only; PostgreSQL uses TLS and a read-only
transaction. Per-symbol history must be no more than seven days stale. Oversized sources
fail instead of silently truncating; default selected-row limit is 2,000,000.

1. Sort by global feature timestamps, keeping all symbols on a given timestamp together.
2. Allocate approximately 70% train, 15% validation, and 15% untouched test timestamps.
   Minimum row counts are 120/30/30; every symbol must occur in every split.
3. Purge train rows whose future label reaches validation, and development/validation rows
   whose future label reaches test. This prevents forward-label boundary leakage.
4. Fit a scaler on training data only. Compare ridge alpha values 0.1, 1, 10, and 100 using
   validation MAE; break ties toward the smaller alpha.
5. Refit the selected alpha and scaler on pre-test development data only. Never fit on test.
6. Evaluate held-out MAE, RMSE, and directional accuracy against zero-return persistence.
   A candidate is eligible for paper evaluation only if test MAE / persistence MAE ≤ 1.0
   and directional accuracy ≥ 0.50. A zero persistence MAE is not automatically accepted.
7. Archive every candidate and its gate result. No gate enables trading, replaces a running
   model, changes risk settings, or promotes a model to live execution.

Repeated scheduled evaluation on evolving holdouts is not independent statistical proof.
Before adopting a candidate, perform additional walk-forward, stress, liquidity, cost,
corporate-action, survivorship, and out-of-sample reviews. Forecast accuracy is not net P&L.

## Risk boundaries and formulas

Let `B0 = 100000 USD`. Limits are fixed to starting capital, not compounded current equity:

```text
portfolio_limit = B0 × 25 / 100 = 25000 USD
asset_limit     = B0 ×  5 / 100 =  5000 USD
```

Configuration dollar and percentage values must agree. Coherent stricter values are allowed;
values above either requested ceiling are rejected. Before any new buy, for each asset `i`:

```text
held_budget_i = shares_i × max(weighted_average_entry_i, latest_mark_i) + entry_fees_i
pending_budget_i = sum(max(reserved_notional, repriced_adverse_notional) + commission)
proposal_budget = shares_proposed × adverse_buy_price + commission
asset_budget_i = held_budget_i + pending_budget_i + proposal_budget_if_same_asset
require every asset_budget_i <= asset_limit
require sum(asset_budget_i) <= portfolio_limit
require pending_cash_reservations + proposal_notional + proposal_commission <= cash
```

Adverse buy price includes slippage, rounded upward to $0.0001; adverse sell price is rounded
downward to $0.0001. Commission is rounded upward to cents. Entry fees count toward the
allocation budgets as well as cash usage. Repricing a reservation can cause its fill to be
rejected. Losses do not free cost-basis headroom; gains count at the larger market value.

These are transactional pre-trade controls, not a guarantee that market movements can never
push an existing holding above a ceiling. Such a drift blocks new exposure; there is no
unrequested forced-deleveraging rule. There is also no implemented stop-loss, maximum daily
loss, sector/correlation cap, short-selling, margin, options, FX, or portfolio hedging logic.

## Asset exposure policy

- USD equities only, explicitly allowlisted; default empty allowlist blocks all new entries.
- Long-only, positive whole-share quantities; no leverage or borrowed cash.
- Multiple fills in one symbol aggregate; splitting an order cannot bypass the asset limit.
- Pending buys consume both asset and portfolio headroom. Other pending/held symbols need
  fresh marks before any new exposure.
- Five fully allocated $5,000 positions exhaust $25,000, but smaller positions may permit
  more than five symbols; no arbitrary additional count ceiling is inferred.
- Exit processing does not require a new-entry forecast. It covers every active position.
  Same-symbol pending buys are cancelled on exit to prevent an old reservation reopening it.

## Profit-protection exit rule

For weighted average entry `E`, current trade price `P`, and full regular-session high `H`:

```text
pullback_pct = 100 × (H - P) / H
sell_entire_position_at_market iff P > E and 1.0 <= pullback_pct <= 1.5
```

Decimal cross-multiplication is used at boundaries. For `E=100` and `H=110`, `P=108.90`
triggers exactly at 1.0%, and `P=108.35` triggers exactly at 1.5%. `P=108.911` does not
trigger; `P=108.339` is beyond the specified band and also does not trigger. `P=E` never
triggers. The check uses gross entry price; execution costs can still produce a net loss
when a small gross gain is sold. Market orders cannot guarantee the displayed trigger price.

The feed must supply the day's high since the regular-session open, including pre-entry
trades. The engine preserves the maximum supplied/observed high across restarts and resets
it for a new XNYS session. Exchange holidays, early closes, and daylight-saving time are
handled by the exchange calendar. Premarket/after-hours execution is not implemented.

**Gap limitation:** this is the literal inclusive band, not an unlimited trailing stop.
If the first observed quote jumps from below 1.0% to beyond 1.5%, no exit triggers. If a
position falls to/below entry, this rule also does not exit. Sampling, outages, missing
quotes, market gaps, costs, and liquidity mean profit protection is not guaranteed.
Changing to a gap-through safety exit or adding a stop-loss requires a deliberate policy change.

## Backtest baseline and event contract

| Parameter | Baseline |
| --- | --- |
| Starting balance, currency, budgets | $100,000 USD; $25,000 total; $5,000 per asset |
| Universe and evaluation dates | Not yet selected; no fabricated historical performance |
| Model horizon and warmup | Next completed daily bar; 60 bars |
| Train/validation/test | 70/15/15 chronological timestamps with forward-label purging |
| Entry threshold | Forecast return > 0.002; explicit causal buy intent |
| Execution | Next supplied event after signal, never same-event signal execution |
| Commission and slippage | 5 bps per side each; adverse slippage; rounded fees |
| Exit | Full-position strict-band daily-high profit protection |
| End of sample | Mark open positions; do not invent forced liquidation |
| Replay outputs | Input/config hashes, fills, equity curve, total return, maximum drawdown |

Daily OHLC bars do not reveal whether high or low occurred first. The replay harness does
not invent intrabar ordering to make protective exits look successful. Supply genuine,
chronologically ordered full quote snapshots and independently generated causal intents.
Example event shape (synthetic symbol and historical timestamp):

```json
[
  {
    "timestamp": "2025-01-06T15:00:00Z",
    "quotes": [
      {
        "symbol": "AAA",
        "price": "100",
        "session_high": "100",
        "timestamp": "2025-01-06T15:00:00Z",
        "currency": "USD",
        "asset_class": "equity"
      }
    ],
    "buy_intents": [
      {
        "order_id": "research-entry-001",
        "symbol": "AAA",
        "quantity": 10,
        "signal_timestamp": "2025-01-03T21:00:00Z",
        "model_training_labels_through": "2025-01-02T21:00:00Z",
        "forecast_return": 0.003
      }
    ]
  }
]
```

Configure your authorized symbols before running; `AAA` is not an investment selection.
The caller must substantiate intent/model metadata and high-water marks; timestamps are
validated, but self-declared metadata is not cryptographic proof of a causal source.

```bash
api-model --config config/trading.local.json replay --events data/events.json \
  --state results/backtest-state.json --output results/backtest-report.json
```

Replay refuses an existing state/report rather than silently resetting or overwriting it.
Use a new experiment path and record the source data/model checksums for each comparison.

## Learned-parameter archive

Every successful training run creates:

```text
results/training/RUN_ID/model.json
results/training/RUN_ID/model.sha256
results/training/RUN_ID/model-card.md
results/training/parameter-archive.jsonl
```

The JSON model and each full append-only JSONL snapshot include:

- Run ID, UTC creation time, source Git commit, Python/library versions.
- Canonical selected-data SHA-256; source date range, row count, and symbol universe.
- Both complete configurations and their SHA-256 hashes.
- Ordered feature names, chosen ridge alpha, intercept, coefficients, scaler means/scales,
  fitted row count, and training-label cutoff.
- Split feature/label boundaries and per-symbol counts; all validation candidate scores.
- Held-out model/persistence metrics, relative MAE, candidate status, and gate outcome.

Run directories are not overwritten. Parameter JSON is checked for finite values before
writing; loading verifies the sidecar SHA-256 and parameter dimensions. Archive appends use
an exclusive lock and fsync. Checksums detect accidental corruption, not hostile edits by
someone who can replace both file and checksum. For a regulated audit, export signed records
to access-controlled, immutable retention storage and reconcile any interrupted partial run.

The GitHub workflow uploads candidates for 30 days; hosted artifacts are not a permanent
archive. This repository is public: artifacts can reveal fitted parameters and symbol
universes. Approve licensing/access or use a private repository/storage arrangement before
connecting proprietary data. Raw market bars and credentials must never be committed.

## Audit and operating checklist

1. Reproduce dependency versions, Git revision, configuration hashes, and selected-data hash.
2. Verify adjusted/complete flags against the vendor, timestamps, universe membership,
   corporate actions, delistings, duplicates, missing rows, and freshness.
3. Confirm strict chronological label purging and that scalers/fits exclude held-out test data.
4. Compare MAE/RMSE/direction metrics and inspect regime-specific and walk-forward results.
5. For each buy, inspect aggregated cost/mark exposure, reserved fees, cash, and idempotency ID.
6. For each exit, reconstruct entry, full-session high, trigger quote, session, band boundaries,
   adverse execution price, fees, net realized P&L, and cancelled same-symbol reservations.
7. Preserve account event history. Validated snapshots, reservations, fills, exits, and risk
   rejections are recorded; never treat missing/stale snapshots as successful monitoring.
8. Test restart, duplicate delivery, concurrent intents, holiday/early-close sessions, stale
   data, band boundaries, gap-through behavior, and configuration mismatch before adoption.
9. Supervise the quote updater/monitor, alert on rejected snapshots, and back up state. POSIX
   atomic JSON persistence is for bounded research/paper use, not unbounded high-frequency
   operation. Production market feeds, broker reconciliation, deployment, alerts, and security
   controls require separate integration and review.
10. Roll back code through a reviewed Git revert if needed; migrate existing account state
    explicitly. A code/config rollback must not erase balances or create duplicate orders.

## Activation status

Code, tests, CI, and the scheduled workflow are versioned. Actual database credentials,
licensed market data, a symbol universe, real-data backtests, private artifact retention,
and any production integration were not supplied. Set the `MARKET_DATABASE_URL` Actions
secret and `ENABLE_AUTOMATED_TRAINING=true` only after those prerequisites are approved.
Manual dispatch is also available on `main`. No live trading is enabled by this project.
