# Finance Integration — Bank-Email Parsing → Live Ledger

The finance module turns transactional emails from Indian banks / cards /
delivery apps into a live spend ledger on the dashboard's Finance page.

It is **self-contained** in this repo — no external service, no separate port:
- DB: `pi/finance.db` (sibling of `calendar.db`, gitignored)
- Code: `pi/finance_db.py`, `pi/finance_parsers.py`, `pi/finance_sync.py`
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
- `alerts@hdfcbank.net`, `alerts@hdfcbank.com` (HDFC savings + credit card —
  credit-card subjects/bodies dispatch to `parse_hdfc_cc`)
- `iobalerts@iob.in`, `noreply@iob.in` (Indian Overseas Bank)
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
| `GET /api/finance/summary` | aggregated month/week/today figures + breakdowns |
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

### B. Server-side OAuth poller (not yet wired)

`finance_sync._fetch_via_oauth()` is a stub. Wire up
`google-api-python-client` + a stored refresh token to run `run_sync()`
unattended (e.g. as a systemd timer on the Pi). Same parsers, same DB.

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
