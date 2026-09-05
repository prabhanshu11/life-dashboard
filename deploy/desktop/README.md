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

## Rules for agents

- `life-dashboard.service` is the dashboard's OWN service and the ONLY unit an
  agent may restart on the desktop (`systemctl --user restart life-dashboard`).
- NEVER touch `star-trek-camera.service` or anything else on the desktop.
- Logs: `ssh desktop 'journalctl --user -u life-dashboard -n 50 --no-pager'`.
