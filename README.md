# AI Trading Bot — PAPER only

This project is a paper-trading simulator. It has no broker integration, no live-trading switch, and no code path that submits real orders. Production serving uses Gunicorn and the Flask WSGI application; `app.run()` is only a local development fallback.

## Local production-style run

Install dependencies with `pip install -r requirements.txt`. Set `PAPER_API_TOKEN` to a secret random value of at least 32 characters and `PAPER_DB_PATH` to an absolute SQLite file path, then run:

```text
gunicorn --workers 1 --threads 4 --bind 0.0.0.0:10000 main:app
```

The dashboard is available at `/` and `/dashboard`; the existing API routes remain `/health`, `/status`, `/signals`, `/positions`, and `/trades`. Missing or weak required configuration causes startup to fail with an error that does not include the token.

## Render configuration

The checked-in `render.yaml` configures a single paid Starter web instance, one Gunicorn worker, a 4-thread worker, health check `/health`, a 1 GB persistent disk mounted at `/var/data`, and an auto-generated `PAPER_API_TOKEN`. It sets `PAPER_DB_PATH=/var/data/paper_trading.sqlite3`; the application rejects a database path outside the configured disk mount. `autoDeployTrigger: off` leaves deployment under your control.

Render Persistent Disks require a paid service; the Free web service filesystem is ephemeral. Only files under the mount path survive restarts and deploys. The single attached disk also means this service must stay at one instance. See [Render Persistent Disks](https://render.com/docs/disks) and the [Render Blueprint reference](https://render.com/docs/blueprint-spec).

## PAPER controls

Risk settings can be configured with `PAPER_STARTING_BALANCE`, `PAPER_CURRENCY`, `RISK_PER_TRADE`, `MAX_DAILY_LOSS`, `MAX_TOTAL_RISK`, `MAX_OPEN_POSITIONS`, `MAX_NOTIONAL_MULTIPLE`, `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT`, `PAPER_FEE_RATE`, `PAPER_SLIPPAGE_RATE`, `MAX_SPREAD_RATE`, `PAPER_ASSUMED_SPREAD_RATE`, and `MAX_DATA_AGE_SECONDS`. yfinance does not provide bid/ask quotes here; spread is therefore an explicit paper estimate, not a measured market spread.

The token-protected PAPER control endpoints are `POST /paper/emergency-stop`, `POST /paper/resume`, `POST /paper/orders`, and `POST /paper/positions/<symbol>/close`. These routes only change simulator state. Emergency STOP is persisted and blocks new positions until explicitly resumed; the daily-loss limit cannot be resumed while still breached. The API token is never included in application log messages or the Render Blueprint.

## Tests

Run `python -m unittest discover -s tests -v`. Tests use temporary SQLite files and do not fetch market data or submit orders.
