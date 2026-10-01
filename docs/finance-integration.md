# Finance Integration — Bank-Email Parsing → Live Ledger

The finance module turns transactional emails from Indian banks / cards /
delivery apps into a live spend ledger on the dashboard's Finance page.

It is **self-contained** in this repo — no external service, no separate port:
- DB: `pi/finance.db` (sibling of `calendar.db`, gitignored)
- Code: `pi/finance_db.py`, `pi/finance_parsers.py`, `pi/finance_sync.py`, `pi/finance_gmail.py`
- API: mounted on the same FastAPI app (`pi/calendar_api.py`)
- UI: the `#page-fin` Bloomberg-terminal panel in `pi/templates/dashboard.html`

## What you see on the Finance page

| Panel | Source |
|-------|--------|
| **This Month · Net Flow** | `income_month − spend_month` from `finance_db.get_summary()` |
| **Burn Rate** | average daily spend so far this month + per-account breakdown |
| **Live Transactions** | newest rows from `transactions` table |
| **Spend by Category · This Month** | `category` group-by, plus the Gmail connect prompt |

The header tag flips from `GMAIL SYNC PENDING` (amber) → `N TXNS SYNCED` (green)
once the DB has any rows.

## Supported senders

`finance_parsers.SENDER_HINTS`:
- `alerts@hdfcbank.net`, `alerts@hdfcbank.com`, `alerts@hdfcbank.bank.in` (HDFC savings + credit card —
  credit-card subjects/bodies dispatch to `parse_hdfc_cc`)
- `iobalerts@iob.in`, `noreply@iob.in`, `iobalerts@iob.bank.in` (Indian Overseas Bank; `.bank.in` = the RBI bank domain move, unverified against his inbox)
- `noreply@swiggy.in`, `no-reply@swiggy.in`
- `noreply@blinkit.com`, `order-update@blinkit.com`

Add a new sender by:
1. Adding to `_DISPATCH` in `finance_parsers.py` with a matcher lambda.
2. Adding a `parse_<source>(sender, subject, body)` function returning a
   transaction dict — or `None` if it doesn't look like one.
3. Appending the address to `SENDER_HINTS` so the Gmail search picks it up.

## API

| Endpoint | Purpose |
|----------|---------|
| `GET /api/finance/summary` | aggregated month/week/today figures + breakdowns + the wall fields |
| `GET /api/finance/wall` | wall FINANCE slide: rolling week vs last week, 30-day series, top categories, last 5, poller state |
| `GET /api/finance/transactions?limit=&days=` | recent transactions |
| `POST /api/finance/transaction` | insert directly (idempotent on `email_id`) |
| `POST /api/finance/parse-email` | parse one raw email and store if a txn |
| `POST /api/finance/sync` | batch: `{messages:[{id,sender,subject,body},…]}` |
| `POST /api/finance/account` | upsert a known account balance |
| `GET /api/finance/gmail-query` | the Gmail search string to use upstream |

Dedup key is the Gmail `email_id` — running the same sync twice is safe.

## Sync paths

### A. Claude-driven (works today)

1. Run `/mcp` in Claude Code → authenticate **claude.ai Gmail**.
2. Ask Claude to sync. It will:
   - Call `GET /api/finance/gmail-query` to get the search string.
   - Search Gmail via the MCP for matching messages.
   - Pull each message's `from / subject / body / id`.
   - `POST /api/finance/sync` with the batch.
3. The Finance page auto-refreshes (poll every 60 s on the page).

### B. Server-side OAuth poller (wired 2026-10-01, desktop timer)

```
life-finance-sync.timer (30 min, 5 min after boot)
  -> .venv/bin/python -m pi.finance_sync          (repo root, desktop)
     -> pi/finance_gmail.py: token file -> Gmail API users.messages list/get (gmail.readonly)
     -> finance_parsers.parse_email -> finance_db (dedup on Gmail id)
     -> ~/.local/state/life-dashboard/finance-sync-last.json {ts, counts, error}
GET /api/finance/summary   (old keys + week, last_week, delta_pct, balance_series, top_categories_week, last_sync, new_since)
GET /api/finance/wall      (only the wall's fields; fixture: docs/wall-fixtures/finance.json)
```

- Query: `from:(<SENDER_HINTS joined by OR>) newer_than:60d -category:promotions` (logged every run,
  stored as `query` in the state file). On 2026-10-02 it matched 251 ids, all `alerts@hdfcbank.bank.in`.
- Incremental: every fetched id goes into `finance.db` table `gmail_seen`, so a run lists all matching
  ids (pages of 50) and only `messages.get`s the unseen ones, inserting each as it is parsed. The state
  file's `cursor {newest_ms, backfill_done}`: the 60-day backfill repeats (cheaply) until one run
  finishes it, then runs list `after:<newest_ms - 1 day>`. `--days N` overrides.
- Quota (fix 2026-10-02): Gmail charges 5 units per `messages.list` and 5 per `messages.get` (full or
  metadata: same price, so `format=full`). The project's limit is 6,000 units / user / minute.
  `finance_gmail.QuotaMeter` paces to `FINANCE_GMAIL_UNITS_PER_MINUTE` (5,000) and stops a run cleanly at
  `FINANCE_GMAIL_UNITS_PER_RUN` (5,000 = ~990 messages); 403 rateLimitExceeded / 429 / 5xx retry after
  5, 10, 20, 40 s (+jitter), max 5 tries. `counts` = {fetched, parsed, inserted, duplicates,
  not_a_transaction, skipped_seen, skipped_units, ids_listed, pages, units, rate_limited, error} is
  written on every run, error or not.
- Exit codes: 0 synced, **3 = no usable token** (missing / revoked: the unit treats it as success and
  the state file says `no_token: ...`), 1 = anything else (see `journalctl --user -u life-finance-sync`).
- Body = the `text/plain` part, else `text/html` stripped. The Gmail receive time fixes the
  timestamp when the alert has no date or only a date. "Avl Bal" in HDFC/IOB alerts updates `accounts`,
  which anchors `balance_series[].balance`; without it the series is `cum_net` (running net flow) only.
- Token: `~/.local/state/life-dashboard/gmail-token.json` (`FINANCE_GMAIL_TOKEN_FILE`), mode 600, refreshed
  in place. The same JSON is in pass at `google/gmail-finance-token`; `scripts/gmail_token_restore.sh`
  recreates the file from pass. Nothing secret is in this (public) repo.

#### One-time setup (his hands, ~5 min)

Google Cloud Console, signed in as mail.prabhanshu@gmail.com:
1. https://console.cloud.google.com/projectcreate : create project `life-dashboard`.
2. APIs & Services > Library > "Gmail API" > Enable.
3. Google Auth Platform (OAuth consent screen) > Get started: app name `Life Dashboard`, support
   email = his address, Audience **External**, contact email = his address, agree, Create.
4. Data access > Add or remove scopes > tick `.../auth/gmail.readonly` > Update > Save.
5. Audience > Test users > Add users > his address > Save.
6. Audience > **Publish app** (status "In production"). Reason: in "Testing" Google expires the
   refresh token after 7 days and the poller stops weekly. Unverified is fine for one user; the
   consent page will say "Google hasn't verified this app": Advanced > Go to Life Dashboard (unsafe).
7. Clients > Create client > Application type **Desktop app**, name `finance-poller` > Create >
   Download JSON (to the desktop's `~/Downloads/`).
8. On the desktop: `cd ~/Programs/life-dashboard && uv run scripts/gmail_authorize.py ~/Downloads/client_secret_*.json`
   Open the printed URL, allow. On the desktop browser the redirect is caught by itself; from
   another machine copy the address of the "can't connect to localhost:8765" page and paste it
   into the script. Then delete the client JSON from Downloads.

After that the next timer run (or `systemctl --user start life-finance-sync.service`) backfills 60
days and the wall's FINANCE slide shows real numbers.

## Categorisation

Categories are inferred from the merchant string + body text by simple regex
rules in `finance_parsers._CATEGORY_RULES`:
`salary · rent · food · groceries · shopping · bills · fuel · transport ·
cash · transfer · uncategorised`. Tune rules as needed — the data shape is
stable, only the regexes change.

## Reset / debug

```bash
sqlite3 ~/Programs/life-dashboard/pi/finance.db \
  "DELETE FROM transactions;"   # wipe and re-sync from Gmail
```

To dry-run a parser against an email you can paste:
```bash
curl -s -X POST http://localhost:8080/api/finance/parse-email \
  -H "Content-Type: application/json" \
  -d '{"sender":"…","subject":"…","body":"…","email_id":"…"}'
```
The response includes the parsed transaction or `status: not_a_transaction`.
