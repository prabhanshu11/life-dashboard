# Desktop deployment (live host since 2026-09-05)

The Life Dashboard runs on the desktop as the systemd USER service
`life-dashboard.service` on port 8090. URL over Tailscale / LAN:
http://100.92.71.80:8090 (day page: http://100.92.71.80:8090/day).
It proxies the camera at http://127.0.0.1:8100 (star-trek-camera, same machine).

## Deploy from the laptop

Push to `master` first, then:

```bash
ssh desktop 'cd ~/Programs/life-dashboard && git pull --ff-only && ./deploy/desktop/deploy.sh'
```

First time only (the repo is not on the desktop yet):

```bash
ssh desktop 'git clone git@github.com:prabhanshu11/life-dashboard.git ~/Programs/life-dashboard && ~/Programs/life-dashboard/deploy/desktop/deploy.sh'
```

Do not use `ssh desktop 'bash -s' < deploy/desktop/deploy.sh`: the script copies
the unit file out of the checked-out repo, so it must run from a clone.

One-time: `loginctl enable-linger prabhanshu` on the desktop so the user service
runs without a login session (already enabled as of 2026-09-05).

## Finance poller (timer)

`deploy.sh` also runs `uv sync` (project `.venv`) and installs
`life-finance-sync.service` + `.timer` (every 30 min, 5 min after boot). It
pulls bank-alert mail with the Gmail API into `pi/finance.db`. Until the
one-time consent (`uv run scripts/gmail_authorize.py <client.json>`, see
`docs/finance-integration.md` §B) each run exits 3 and logs "Gmail not
connected"; that is expected. Check it:

```bash
ssh desktop 'systemctl --user list-timers | grep finance; cat ~/.local/state/life-dashboard/finance-sync-last.json; journalctl --user -u life-finance-sync -n 20 --no-pager'
```

The agent rules below allow restarting `life-dashboard` and starting
`life-finance-sync.service` (its own one-shot).

## Statements poller (timer)

`deploy.sh` also installs `life-statements-sync.service` + `.timer` (daily 07:10; the
service pulls a finance sync first). It downloads statement / invoice attachments into
`~/.local/state/life-dashboard/statements/<source>/` and opens protected PDFs with
`pass show finance/statements/<source>` from the passphrase-less finance sub-store
`~/.password-store-finance` (one-time: `deploy/desktop/finance-substore-init.sh`; why and the
trade-off: `docs/finance-integration.md` §C). Check it:

```bash
ssh desktop 'cat ~/.local/state/life-dashboard/statements-sync-last.json; curl -s 127.0.0.1:8090/api/statements/locked'
```

Agents may start `life-statements-sync.service` (its own one-shot).

## Rules for agents

- `life-dashboard.service` is the dashboard's OWN service and the ONLY unit an
  agent may restart on the desktop (`systemctl --user restart life-dashboard`).
- NEVER touch `star-trek-camera.service` or anything else on the desktop.
- Logs: `ssh desktop 'journalctl --user -u life-dashboard -n 50 --no-pager'`.
