# AI Trading Bot — PAPER only

This project is a paper-trading simulator. It has no broker integration, no live-trading switch, and no code path that submits real orders. Production serving uses Gunicorn and the Flask WSGI application; `app.run()` is only a local development fallback.

## Local production-style run

Install dependencies with `pip install -r requirements.txt`. Set `PAPER_API_TOKEN` to a secret random value of at least 32 characters and `PAPER_DB_PATH` to an absolute SQLite file path, then run:

```text
gunicorn --workers 1 --threads 4 --bind 0.0.0.0:10000 main:app
```

The dashboard shell is available at `/` and `/dashboard`. Enter the existing `PAPER_API_TOKEN` to view account data; the browser keeps it in session storage for that tab and does not put it in a URL. The `/status`, `/signals`, `/positions`, and `/trades` API routes require an `Authorization: Bearer <PAPER_API_TOKEN>` header. `/health` remains public for Render health checks and omits balances, positions, and risk amounts. Missing or weak required configuration causes startup to fail with an error that does not include the token.

## Render configuration

The checked-in `render.yaml` configures a single paid Starter web instance, one Gunicorn worker, a 4-thread worker, health check `/health`, a 1 GB persistent disk mounted at `/var/data`, and an auto-generated `PAPER_API_TOKEN`. It sets `PAPER_DB_PATH=/var/data/paper_trading.sqlite3`; the application rejects a database path outside the configured disk mount. `autoDeployTrigger: off` leaves deployment under your control.

Render Persistent Disks require a paid service; the Free web service filesystem is ephemeral. Only files under the mount path survive restarts and deploys. The single attached disk also means this service must stay at one instance. See [Render Persistent Disks](https://render.com/docs/disks) and the [Render Blueprint reference](https://render.com/docs/blueprint-spec).

For isolated Free-tier testing, use the separate `render-free.yaml` Blueprint on branch `paper-trading-integration`. It creates a service named `ai-trading-bot-free`, uses `PAPER_ALLOW_EPHEMERAL_DB=true` and `/tmp/paper_trading.sqlite3`, and has no persistent disk. SQLite state, including paper positions and trade history, is lost when the Free service restarts, spins down, or redeploys. The paid `render.yaml` remains unchanged.

## PAPER controls

Risk settings can be configured with `PAPER_STARTING_BALANCE`, `PAPER_CURRENCY`, `RISK_PER_TRADE`, `MAX_DAILY_LOSS`, `MAX_TOTAL_RISK`, `MAX_OPEN_POSITIONS`, `MAX_NOTIONAL_MULTIPLE`, `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT`, `PAPER_FEE_RATE`, `PAPER_SLIPPAGE_RATE`, `MAX_SPREAD_RATE`, `PAPER_ASSUMED_SPREAD_RATE`, and `MAX_DATA_AGE_SECONDS`. The account and dashboard currency default to `EUR`; set `PAPER_CURRENCY` explicitly to another currency when needed. yfinance does not provide bid/ask quotes here; spread is therefore an explicit paper estimate, not a measured market spread.

Yahoo Finance data is cached independently for US100 and US500. It requests 2-minute bars first, then 1-minute bars if the 2-minute response is empty. If neither Yahoo interval returns bars (or Yahoo fails with a non-rate-limit error), the app can use Massive index minute bars when `MASSIVE_API_KEY` is configured. The fallback maps US100 to `I:NDX` and US500 to `I:SPX`; it never substitutes a different instrument. The dashboard displays the actual source and interval used.

Massive requires an API key and appropriate index data entitlement. Its current individual Indices plans list Basic as end-of-day data, Starter as 15-minute delayed, and Advanced as real-time; the existing 180-second freshness limit blocks execution on stale or delayed bars. Massive's market-data terms restrict non-display use and derived trading strategies unless separately licensed. The integration therefore requires both `MASSIVE_STRATEGY_LICENSE_CONFIRMED=true` and `MASSIVE_PROVIDER_COSTS_CONFIRMED=true`. Set the first only after confirming written rights for this strategy and the second only after explicitly accepting the provider's applicable usage charges. No key or confirmation flag is stored in source control or set by the Blueprint. Without both flags, behavior remains Yahoo-only.

The monitor initiates at most one history fetch per symbol per `YF_MIN_FETCH_INTERVAL_SECONDS` (default `60`) and blocks fetches for all symbols during provider cooldown. HTTP 429 responses trigger exponential backoff with jitter from `YF_BACKOFF_BASE_SECONDS` (default `60`) up to `YF_BACKOFF_MAX_SECONDS` (default `900`); an exposed `Retry-After` value is honored. The dashboard reports the last fetch error, live candle age, and time until the next permitted request. Data older than `MAX_DATA_AGE_SECONDS` (default `180`) is displayed as stale and cannot be used to open or automatically close a position. A last-good quote may remain visible for diagnostics, but stale quote prices are excluded from risk updates and execution decisions.

The PAPER account read APIs and control endpoints require the bearer token. Control endpoints are `POST /paper/emergency-stop`, `POST /paper/resume`, `POST /paper/orders`, and `POST /paper/positions/<symbol>/close`; these routes only change simulator state. Emergency STOP is persisted and blocks new positions until explicitly resumed; the daily-loss limit cannot be resumed while still breached. The API token is never included in application log messages, URLs, or the Render Blueprint.

## Tests

Run `python -m unittest discover -s tests -v`. Tests use temporary SQLite files and do not fetch market data or submit orders.
