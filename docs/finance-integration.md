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
- Orders (lane orders-1002): `noreply@zomato.com`, `auto-confirm@amazon.in`, `order-update@amazon.in`,
  `shipment-tracking@amazon.in`, `return@amazon.in`, `payments-messages@amazon.in` (see "Orders" below).
  Changing `SENDER_HINTS` re-opens the 60-day backfill once (cursor `senders` signature).

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
  stored as `query` in the state file). On 2026-10-02 it matched 251 ids, all `alerts@hdfcbank.bank.in`
  (225 transactions; the 26 others: RM missed calls, e-mandate registrations, declined payments).
- Incremental: every fetched id goes into `finance.db` table `gmail_seen`, so a run lists all matching
  ids (pages of 50) and only `messages.get`s the unseen ones, inserting each as it is parsed. The state
  file's `cursor {newest_ms, backfill_done}`: the 60-day backfill repeats (cheaply) until one run
  finishes it, then runs list `after:<newest_ms - 1 day>`. `--days N` overrides.
- Quota (fix 2026-10-02): Gmail charges 5 units per `messages.list` and 5 per `messages.get` (full or
  metadata: same price, so `format=full`). The console shows 6,000 / user / minute, but live runs were
  refused at 520 and 580 documented units in a rolling minute (~1/10 of it), so
  `finance_gmail.QuotaMeter` paces to `FINANCE_GMAIL_UNITS_PER_MINUTE` (500) and stops a run cleanly at
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

## Orders (Zomato, Swiggy, Amazon, Blinkit) — lane orders-1002, 2026-10-02

His words: "incorporate swiggy, zomato, amaon orders also".

Sender discovery, 120 days to 2026-10-02 (headers only, all in Gmail's Updates tab):

| Brand | Sender | Subjects (count) | Used |
|---|---|---|---|
| Zomato | `noreply@zomato.com` | "Your Zomato order from <restaurant>" (4, one per order, sent on delivery; ORDER ID, items, "Total paid") | yes |
| Zomato | `noreply@mailers.zomato.com` | marketing (21); plus Gold welcome + login alert from `noreply@zomato.com` (2) | no |
| Swiggy / Instamart / Dineout | none | 0 mails from any swiggy address; "swiggy" appears only in HDFC card alerts (14) | card rows |
| Blinkit / Zepto | none | 0 mails; Blinkit appears only in HDFC card alerts (15) | card rows |
| Amazon | `auto-confirm@amazon.in` | "Ordered: ..." (24; Order #, items, "Total N INR") | yes |
| Amazon | `shipment-tracking@amazon.in` | Shipped (30), Out for delivery (17), Arriving Today OTP (12) | status |
| Amazon | `order-update@amazon.in` | Delivered (25), Problem during delivery / attempted (13), Item cancelled (1), Amazon Fresh (3) | status |
| Amazon | `return@amazon.in` | "Your refund for ..." (4), "Your return of" (1), Replacement (1) | refunds |
| Amazon | `payments-messages@amazon.in` | "Refund on order <id>" (1) | refunds |
| Amazon | `no-reply@amazon.in`, `account-update@`, `services@` | return surveys, sign-in, warranty (10) | no |
| Amazon Pay | `no-reply@amazonpay.in` | "Rs N was paid on Amazon.in" (9, Amazon Pay balance), refund / cashback (16) | not yet |

**One ledger row per order.** `finance_db.orders` keeps one row per order (`source:order_id`) and per
refund (`source:order_id:refund:<rupees>`). The first mail with an amount makes the money count; every
later mail (shipped, delivered, cancelled) only updates `order_status` / `items`. The order's time is its
earliest mail, `ts_last` its latest. Cancelled before it was paid for -> nothing counts (and refunds of it
are ignored). Refund mails make a CREDIT row with the order's category.

**No double counting with the bank (the link rule, `finance_db.link_counterpart`).** An order (or refund)
and a bank/card row (`hdfc`, `hdfc_cc`, `iob`, `statement`) are the same money when: same direction,
amounts within 1 rupee, the bank row within 2 days of the order's mail span (placed .. last shipment /
delivery mail; Amazon charges the card on dispatch), the bank row has no `matched_email_id` yet, and the
bank row does not name a different brand (an Amazon order never takes a SWIGGY card row). Closest brand
match, then amount, then time wins. Linked -> the bank row gets `merchant`, `items`, `category`,
`order_id`, `order_status`, `payment` and `matched_email_id` (the order mail) and no order row exists.
Not linked (COD, Amazon Pay balance, wallet, a card without alerts) -> a `source='order'` row counts it.
The same function runs after an order row is inserted AND after a bank alert / statement row is inserted,
so the arrival order does not matter; `relink_orders()` retries the standing order rows after every sync.
Payments marked `amazon_pay_balance` / `wallet` never link.

Also fixed on the way: HDFC card alerts now read the merchant from "towards X on" / "From Merchant: X"
and treat "transaction reversal" as a CREDIT (before: every reversal counted as spend). Rows stored
before the fix are repaired on every sync (`repair_card_rows`, only rows with no merchant).

**Wall / summary.** `/api/finance/summary` and `/api/finance/wall` carry
`orders: {week: {food, groceries, shopping, n}, last_week: {...}, top_merchants_week: [{merchant, total, n}],
linked_to_bank, order_rows}`: rolling 7 days, order-linked rows plus card rows that name a brand (Swiggy,
Blinkit send no mail), net of refunds. `top_categories_week` picks up the enriched categories.

## Categorisation

Categories are inferred from the merchant string + body text by simple regex
rules in `finance_parsers._CATEGORY_RULES`:
`salary · rent · groceries · food · shopping · bills · fuel · transport ·
cash · transfer · uncategorised` (groceries is tried before food so "Swiggy Instamart" is groceries;
card reversals are `refund`). Tune rules as needed — the data shape is
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

## C. Statements poller (attachments, wired 2026-10-02, lane statements-1002)

His words (2026-10-02): "get all the attachments and the account statements and various statements of
various apps. Some of them would be password protected, so we will have to create a database and store
those passwords so that those files can be opened as and when they arrive".

```
life-statements-sync.timer (daily 07:10; the unit Wants+After life-finance-sync.service)
  -> .venv/bin/python -m pi.statements_sync --days 90
     -> pi/statements_registry.yaml: sender -> source id, folder, pass entry, hint, parser (NO secrets)
     -> Gmail (same paced client + unit counter as finance_gmail): registry senders with attachments,
        each message fetched once, each kept attachment downloaded once (attachments_seen)
     -> ~/.local/state/life-dashboard/statements/<source>/<YYYY-MM-DD>-<filename>   (dir 700, file 600)
     -> encrypted PDF? empty password, else `pass show finance/statements/<source>` (first line)
        -> <name>.open.pdf beside it; no entry / wrong password -> status locked + hint (retried once a run)
     -> hdfc_savings / hdfc_cc parsers -> transactions (source 'statement', email_id stmt:<sha256>:<n>),
        except rows that match an e-mail alert (direction, amount, date +-1 day, account HDFC*): those
        are linked in statement_rows.matched_email_id instead; card bill payments are skipped
     -> discovery: has:attachment filename:pdf, headers + filenames only, into attachment_discovery
     -> ~/.local/state/life-dashboard/statements-sync-last.json
GET  /api/statements          registry + per-source last file + status counts + locked + discovery
GET  /api/statements/locked   sources waiting for a password: hint + `pass insert ...` command
POST /api/statements/sync     starts life-statements-sync.service (non-blocking)
GET  /api/finance/wall        + statements {locked, locked_sources, total, last, last_sync, error}
```

Passwords live ONLY in `pass` on the desktop (`pass insert finance/statements/<id>`, first line = the
PDF password). `pass` runs with `--batch --pinentry-mode error` and a 20 s timeout, so the timer never
pops a pinentry and never hangs on a gpg lock; such a failure shows as `locked` with the reason.
Report of what is locked: `uv run python -m pi.statements_sync --report` or `/api/statements/locked`.
Add a sender: append to the registry (match.from + optional subject/filename regexes, skip.* for mails
that must never be downloaded, e.g. IOB PIN letters) and re-run.
