# IG Trader

IG Trader is a safety-first Python trading platform that integrates with the
IG Group REST API. It is under active development and is not a promise of
profitability or a recommendation to trade.

## Safety model

The system is designed to fail closed: missing, stale, malformed, or ambiguous
broker, account, market, risk, or position-ownership state blocks automated
trading. IG remains the source of truth for open broker positions, and local
strategy ownership must be known before automation can proceed.

Orders are never placed merely to test code. Credentials and tokens must not be
committed, logged, or shared.

## Execution modes

- `NO_EXECUTION` is the mode of the currently deployed Azure worker. It has no
  trading-worker authority.
- `SHADOW_DEMO` permits IG Demo market reads and hypothetical execution only.
  It is permanently `authorized=false` and `order_authority=false`.
- `DEMO_EXECUTION` is disabled and requires separate controlled qualification.
- `LIVE_EXECUTION` is disabled. Live trading requires explicit Afif approval.

Broker execution is disabled. The frozen V1 Shadow scope is limited to:

- `CS.D.EURGBP.MINI.IP`
- `CS.D.EURUSD.CEFM.IP`
- `CS.D.GBPUSD.MINI.IP`

Research and Paper qualification remain separate from Shadow mode. The project
does not promise profitability.

## Development setup

Windows with Python and Poetry is the supported local environment.

```powershell
poetry check --lock
poetry sync --no-interaction --no-ansi
poetry run pip check
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check .
poetry run pre-commit run --all-files
poetry run python tools/scan_tracked_secrets.py
```

Do not run the bot against a broker account as a code test. Keep
`PAPER_TRADING=true` unless a controlled Demo execution has been explicitly
authorized.

## Contributing

Work on a feature branch, make focused changes with tests, run the validation
commands above, and open a pull request for review. Do not push or merge to
`main`, commit local databases or credentials, or weaken a safety control to
make a test pass.
