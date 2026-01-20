# Life Dashboard

Self-hosted calendar system on Raspberry Pi Zero 2 W with VPS reverse proxy and Claude Code integration.

## Architecture

```
Pi Zero 2 W (home WiFi)
    │
    └── SSH tunnel (autossh) ──► VPS (72.60.218.33:8081)
                                    │
                                    └── nginx reverse proxy
                                            │
                                            └── life.prabhanshu.space
```

- **Pi runs**: FastAPI calendar API on localhost:8080
- **autossh**: Maintains persistent SSH tunnel, VPS:8081 -> Pi:8080
- **VPS nginx**: Reverse proxy with SSL termination

## Quick Reference

### URLs
- **Production**: https://life.prabhanshu.space
- **Local dev**: http://localhost:8080

### Default Calendars
- `computer` - Work/tech (default, #4285f4)
- `family` - Family events (#4caf50)
- `birthdays` - Birthday reminders (#9c27b0)
- `1dollar` - 1$ Challenge (#ff9800)

## Development

### Run locally
```bash
cd ~/Programs/life-dashboard/pi
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn calendar_api:app --reload --port 8080
```

### Test API
```bash
# Health check
curl http://localhost:8080/api/health

# List calendars
curl http://localhost:8080/api/calendars

# Create event
curl -X POST http://localhost:8080/api/events \
  -H "Content-Type: application/json" \
  -d '{"title": "Test", "calendar_id": "computer", "start_time": "2026-01-20T10:00:00", "end_time": "2026-01-20T11:00:00"}'
```

### Dashboard
Open http://localhost:8080/ - add `?days=3` for 3-day view.

## Deployment

### First-time Pi Setup
1. Flash Raspberry Pi OS Lite (64-bit) to SD card
2. Configure WiFi: `wpa_supplicant.conf` in boot partition
3. Enable SSH: empty `ssh` file in boot partition
4. Boot Pi, find IP, SSH in
5. Copy and run setup script:
   ```bash
   scp scripts/setup-pi.sh pi@<PI_IP>:~/
   ssh pi@<PI_IP> 'chmod +x setup-pi.sh && ./setup-pi.sh'
   ```

### Deploy updates
```bash
./scripts/deploy.sh pi@<PI_IP>
```

### VPS Setup (one-time)
See `scripts/vps-nginx-setup.sh` for nginx config reference.
Deploy via vps_bootstrap repo (GitHub Actions).

## Claude Skill

Use `/calendar` in Claude Code:
- "add meeting tomorrow at 3pm"
- "what's on my calendar this week"
- "block 2 hours for deep work tomorrow morning"

Skill location: `~/.claude/skills/calendar/SKILL.md`

## Files

```
life-dashboard/
├── pi/                          # Deployed to Pi
│   ├── calendar_api.py          # FastAPI server
│   ├── database.py              # SQLite operations
│   ├── requirements.txt         # Python deps
│   ├── templates/
│   │   └── dashboard.html       # Vertical monitor view
│   └── calendar.service         # systemd unit
├── scripts/
│   ├── setup-pi.sh              # Pi bootstrap
│   ├── deploy.sh                # Deploy to Pi
│   └── vps-nginx-setup.sh       # VPS config reference
└── CLAUDE.md                    # This file

~/.claude/skills/calendar/       # Claude skill
├── SKILL.md
└── scripts/
    ├── add-event.sh
    ├── list-events.sh
    └── analytics.sh
```

## Troubleshooting

### Tunnel down
```bash
# On Pi
sudo systemctl status calendar-tunnel
sudo journalctl -u calendar-tunnel -f

# On VPS
ss -tlnp | grep 8081
```

### API not responding
```bash
# On Pi
sudo systemctl status calendar
curl http://127.0.0.1:8080/api/health
```

### VPS nginx issues
```bash
# On VPS (read-only!)
sudo nginx -t
tail -f /var/log/nginx/error.log
```
