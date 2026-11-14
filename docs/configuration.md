# Configuration and model integration

## Scope

`config/trading.example.json` is a versioned template for a future model. Copy it to the ignored `config/trading.local.json` before setting local values. No loader, validation script, strategy, broker adapter, or order-execution service is included.

All capital and position-limit values are intentionally unset. They must be chosen by the repository owner; this scaffold supplies no recommended allocation or risk level.

## Parameters

| Field | Required value or meaning |
| --- | --- |
| `schema_version` | Integer `1`; a future loader must reject unsupported versions. |
| `mode` | String `paper`. No live mode is implemented or authorized by this scaffold. |
| `live_trading_enabled` | Boolean `false`. This is a declaration, not an implemented trading safeguard. |
| `capital.initial_capital` | Finite number greater than zero: starting simulated account capital in the configured currency. Initially `null`. |
| `capital.currency` | Uppercase three-letter ISO 4217 currency code for all monetary amounts. Initially `null`. |
| `position_limits.max_position_value` | Finite number greater than zero: maximum absolute market value of the aggregate position in one instrument, in the configured currency. Initially `null`. |
| `position_limits.max_position_pct` | Finite number greater than zero and at most `100`: maximum value of one instrument's aggregate position as a percentage of current simulated account equity. Use percentage points, not a decimal fraction. Initially `null`. |
| `position_limits.max_open_positions` | Positive integer: maximum number of distinct instruments with a nonzero simulated position. Initially `null`. |

A missing or `null` required value means **configuration incomplete**. Never interpret it as unlimited exposure, zero capital, or approval to trade. Multiple fills in the same instrument count as one aggregate position, not separate positions.

## Requirements for the future model

The following checks are specifications, **not code implemented in this repository**:

1. Explicitly load the intended local configuration. Reject malformed JSON, unsupported schema versions, missing fields, null required settings, wrong types, non-finite numbers, and out-of-range values before any simulation or order handling.
2. Require `mode == "paper"` and `live_trading_enabled == false`. Reject other values rather than silently enabling live execution. Never connect a broker based only on these flags.
3. Use the configured starting capital only when initializing a simulated account. Do not reset equity on each run without an explicit simulation reset.
4. Before each simulated order, calculate the resulting aggregate instrument position, equity, and number of open positions, including existing pending simulated orders and the proposed order.
5. Apply both per-instrument value limits. The allowed absolute position value is the smaller of `max_position_value` and `current_equity * max_position_pct / 100`. Reject orders that breach either limit or the open-position count. Reject new exposure when equity or required prices are unavailable or invalid.
6. Define cash, fees, slippage, leverage, short-selling, market-data, and any currency-conversion rules explicitly. These behaviors are not implemented here; do not infer support from the template.
7. Test missing configuration, invalid values, boundary values, aggregated fills, pending orders, and limit breaches using simulated data before connecting the model to any external system.

## Completing the setup

- Add the existing trading model's source and its actual dependencies.
- Map its capital and position-limit settings to this documented format, or deliberately update this format to match the model.
- Implement and test the loader and checks above in that model.
- Keep credentials out of the configuration and Git history. Ignored files can still be committed explicitly; `.gitignore` does not remove secrets already tracked.

Live trading requires a separate design, security review, broker integration, tests, and explicit authorization. This scaffold does not provide those components.
