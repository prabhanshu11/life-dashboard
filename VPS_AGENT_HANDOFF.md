# VPS Agent Handoff - 2026-01-20

## Current Status: PARTIAL SUCCESS

- Repo made PUBLIC
- nginx config DEPLOYED to VPS
- SSL PENDING (needs DNS A record)
- WiFi creds stored locally in `.pi-wifi-creds` (gitignored)

## What Was Done

### 1. Created VPS Deployment Infrastructure
- `deploy/nginx/life.prabhanshu.space.conf` - nginx reverse proxy config
- `deploy/vps-deploy.sh` - Deployment script (copies nginx config, runs certbot)
- `.github/workflows/deploy-vps.yml` - GitHub Actions CI/CD workflow

### 2. Configured GitHub Secrets
- `SSH_PRIVATE_KEY` - Unified VPS deploy key (from `pass show github/vps-deploy-key` on desktop)
- `VPS_HOST` - 72.60.218.33
- `VPS_USERNAME` - root

### 3. Committed and Pushed
- Commit: `21294dd` - "Add Life Dashboard calendar system"
- All project files including Pi code, scripts, and VPS deployment

## Resolved Blockers

### ✅ Blocker 1: Repo is Private - RESOLVED
- Made repo PUBLIC via `gh repo edit --visibility public`
- VPS can now clone via HTTPS

### ⏳ Blocker 2: DNS Not Configured - PENDING USER ACTION
- **Issue**: `life.prabhanshu.space` doesn't resolve
- **Impact**: certbot can't verify domain for SSL
- **VPS Status**: nginx config IS deployed, just no SSL yet
- **Solution**: Add A record in Hostinger DNS dashboard
  - Type: A
  - Name: calendar
  - Points to: 72.60.218.33
- **After DNS**: Run `gh workflow run deploy-vps.yml` to complete SSL setup

## User's Broader Vision (from conversation)

The user clarified this is part of a larger home server setup:

```
┌─────────────────────────────────────────────────────────────────┐
│                     Raspberry Pi Zero 2 W                        │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │   Calendar   │  │    CCTV      │  │     NAS      │           │
│  │     API      │  │   Viewer     │  │   Storage    │           │
│  └──────────────┘  └──────────────┘  └──────────────┘           │
│         │                                                        │
│         │         ┌──────────────┐  ┌──────────────┐            │
│         │         │  Ad Blocker  │  │   Hotspot    │            │
│         │         │  (Pi-hole?)  │  │   (local)    │            │
│         └─────────┴──────────────┴──┴──────────────┘            │
│                            │                                     │
│        WiFi to GeoFiber ←──┴──→ Hotspot for local devices       │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                        SSH Tunnel (autossh)
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    VPS (72.60.218.33)                           │
│                                                                  │
│  nginx ──► life.prabhanshu.space ──► Pi:8082 (tunnel)       │
│                                                                  │
│  Also hosts: prabhanshu.space, avantiterraform.com              │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
                    Worldwide Access via HTTPS
                (Phone calendar in "My Zone" with GitHub auth)
```

### Key Points from User:
1. Pi replaces Tailscale for some use cases (not all - still need Tailscale for SSH to desktop)
2. Pi should connect to GeoFiber AND create its own hotspot (dual WiFi?)
3. Local devices connect to Pi's hotspot for local access
4. Global sync goes through VPS tunnel

### User Decisions Made:
1. **WiFi Mode**: Client only (no hotspot for now)
2. **WiFi Network**: JioFiber - creds in `.pi-wifi-creds` (gitignored)
3. **Priority**: Calendar first, then expand to full home server
4. **Repo Visibility**: Made public (simpler deployment)

## Files Changed

| File | Status | Notes |
|------|--------|-------|
| `deploy/nginx/life.prabhanshu.space.conf` | NEW | nginx config |
| `deploy/vps-deploy.sh` | NEW | Deployment script |
| `.github/workflows/deploy-vps.yml` | NEW | CI/CD workflow |
| `vps-setup/TASK.md` | UPDATED | Progress tracking |
| `CLAUDE.md` | EXISTING | Project docs |
| `pi/*` | EXISTING | Calendar API code |

## Next Steps for Master Agent

1. ✅ ~~Resolve repo visibility~~ - Made public
2. ⏳ **Add DNS record** - Guide user to Hostinger dashboard for life.prabhanshu.space → 72.60.218.33
3. ⏳ **Complete SSL** - After DNS: `gh workflow run deploy-vps.yml`
4. ⏳ **Flash Pi SD card** - Use rpi-imager with creds from `.pi-wifi-creds`
5. ⏳ **Boot Pi and run setup** - `scp scripts/setup-pi.sh pi@<IP>:~/` then run it
6. ⏳ **Add Pi SSH key to VPS** - For autossh tunnel
7. ⏳ **Deploy calendar to Pi** - `./scripts/deploy.sh pi@<IP>`
8. ⏳ **Test end-to-end** - `curl https://life.prabhanshu.space/api/health`

## VPS Read-Only Commands Used
- `ls /etc/nginx/sites-enabled/`
- `cat ~/.ssh/authorized_keys`
- `ls /var/www/`
- `git remote -v` (in /var/www/prabhanshu.space)

All VPS access was read-only per CLAUDE.md rules.
