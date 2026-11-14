# Priya: trading model configuration scaffold

Starter documentation and configuration for a future trading model. This repository does **not** yet contain a trading strategy, backtest engine, broker connection, or order-execution code.

## Current status

- The example configuration selects paper mode and sets `live_trading_enabled` to `false`.
- Initial capital, currency, and position limits are deliberately `null`; no financial values have been assumed.
- The configuration is a template, not a runnable system. Its flags do not enforce safety until a model implements the checks described in [the configuration guide](docs/configuration.md).

## Repository structure

```text
README.md
config/trading.example.json
docs/configuration.md
.gitignore
```

## Configure a local copy

```bash
git clone https://github.com/priyapondi10-tech/Priya.git
cd Priya
cp config/trading.example.json config/trading.local.json
```

1. Edit `config/trading.local.json` with your chosen capital, currency, and position limits.
2. Keep `mode` as `paper` and `live_trading_enabled` as `false`.
3. Read the parameter definitions and integration requirements in [docs/configuration.md](docs/configuration.md).
4. Add the existing trading-model source separately and implement validation and position-limit checks before using it with this configuration.

If Python 3 is installed, check JSON syntax with:

```bash
python3 -m json.tool config/trading.local.json
```

This command checks syntax only; it does not validate risk limits or run a model. A `null` setting means **not configured**, never zero, unlimited, or permission to trade.

## Secrets and local files

This repository is public. Never commit API keys, broker credentials, account identifiers, or private trading data. Local configuration, common credential files, and local environment files are listed in `.gitignore`; ignoring files is not a guarantee against disclosure.

No live-trading setup or investment recommendation is included.
