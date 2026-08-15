# G1-01 IG Demo read-only authentication diagnostic

Status: isolated diagnostic implementation

Execution authority: none

Environment: IG Demo only

## Purpose

`tools.ig_auth_diagnostic` proves the IG REST v2 and Lightstreamer authentication
path without importing the trading bot, strategy, risk engine, or execution
adapter. It creates a v2 session, verifies the active and configured accounts,
reads the configured market, receives one price quote, deliberately disconnects,
re-authenticates once, and restores the same read-only subscription.

The REST base is fixed to `https://demo-api.ig.com/gateway/deal`. A request is
rejected before transmission unless it matches this allow-list:

- `POST /session` version 2
- `GET /session` version 1
- `GET /accounts` version 1
- `GET /markets/{epic}` version 3
- `GET /markets?searchTerm=...` version 1
- `DELETE /session` version 1

Every method on `/positions`, `/positions/otc`, `/workingorders`, and
`/workingorders/otc` is outside the allow-list. The test suite proves that every
order create, update, and delete form is blocked before the HTTP client is called.

The implementation follows the IG v2 REST token and streaming credential
formats documented in the [IG REST guide](https://labs.ig.com/rest-trading-api-guide.html),
[session reference](https://labs.ig.com/reference/session.html), and
[streaming guide](https://labs.ig.com/streaming-api-guide.html). The dynamic
Lightstreamer endpoint is taken from the session response; the active account is
the Lightstreamer user; and only `CST-...|XST-...` is accepted as the streaming
password. OAuth bearer tokens are rejected.

## Configuration contract

The diagnostic reads only these Demo settings from the process environment or
the repository `.env` file:

- `IG_API_KEY`
- `IG_IDENTIFIER`
- `IG_PASSWORD`
- one consistent account value in `IG_ACCOUNT_ID`, `IG_ACCOUNT_NUMBER`, or
  `IG_SERVICE_ACC_NUMBER`
- optional `IG_DEMO`, which must be true
- optional `IG_BASE_URL`, which must have the Demo hostname
- optional `PAPER_TRADING`, which must be true

Any key whose name begins with `IG_LIVE` blocks startup before credential values
are loaded. A LIVE hostname, a false Demo flag, false paper-trading state,
missing setting, or conflicting account settings also blocks startup.

No credential, session token, OAuth token, or complete account identifier is
written to stdout or evidence. Account values use a run-scoped one-way
fingerprint so configured/active equality remains visible without a reusable
identifier hash.

## Run the primary diagnostic

Place: Windows PowerShell, in the repository directory.

```powershell
poetry run python -m tools.ig_auth_diagnostic --environment demo --session-version 2 --epic CS.D.EURGBP.MINI.IP --output .runtime/evidence/g1-auth-diagnostic.json
```

This performs only the allow-listed operations above. The safe result prints a
classification and the two report paths. Stop if the command prints any
classification other than `PASS`; inspect only the sanitized reports and do not
retry by changing credentials blindly.

Expected files:

- `.runtime/evidence/g1-auth-diagnostic.json`
- `.runtime/evidence/g1-auth-diagnostic.md`

## Development-only `trading-ig` comparison

The comparison is intentionally not a project dependency and is not a
production adapter. Its injected requests session enforces the same host and
endpoint allow-list. It creates a v2 session, fetches accounts, and fetches the
configured market; it never calls a position or working-order method.

`trading-ig 0.0.24` pins Lightstreamer `1.0.3`, while this project requires
Lightstreamer `2.2.2`. Do not install it into the Poetry environment. Create a
disposable environment under `.runtime` instead:

```powershell
py -3.13 -m venv .runtime/g1-trading-ig-venv
```

Install the reference package into that disposable environment:

```powershell
.runtime/g1-trading-ig-venv/Scripts/python.exe -m pip install trading-ig==0.0.24
```

Then run the self-contained comparison:

```powershell
.runtime/g1-trading-ig-venv/Scripts/python.exe tools/ig_auth_trading_ig_reference.py --environment demo --session-version 2 --epic CS.D.EURGBP.MINI.IP --output .runtime/evidence/g1-auth-trading-ig-reference.json
```

This comparison must not be imported by `src/ig_trader`, added to the production
adapter, or used to justify order execution without a separate architecture and
human-authority decision.

## Fault injection and tests

Invalid API key, invalid credentials, KYC/agreement restriction, missing
preferred account, disabled preferred account, active-account mismatch, missing
CST/XST, OAuth-as-stream-token, invalid REST token/401, connection timeout,
malformed response, stale quote, forced disconnect, bounded re-authentication,
secret redaction, and the endpoint allow-list are tested without contacting IG.

```powershell
poetry run pytest -q tests/test_ig_auth_diagnostic.py
```

On Windows systems where Pytest cannot create its default temporary symlink,
use a new path inside `.runtime`:

```powershell
poetry run pytest -q --basetemp .runtime/pytest-g1-focused tests/test_ig_auth_diagnostic.py
```

## Forbidden actions

- Do not use `--environment live`; the parser rejects it.
- Do not change `IG_BASE_URL` to the LIVE host.
- Do not set `PAPER_TRADING=false`.
- Do not add or call position or working-order endpoints.
- Do not paste credentials, tokens, complete account identifiers, or raw broker
  responses into reports or chat.
- Do not start the main bot as part of this diagnostic.
- Do not treat a `trading-ig` result as production-adapter approval.
