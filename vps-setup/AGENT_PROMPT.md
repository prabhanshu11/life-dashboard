# VPS Setup Agent Prompt

Copy this entire prompt to start a new Claude Code session for VPS setup.

---

## Prompt for VPS Agent

```
I need you to set up nginx reverse proxy on my VPS for the Life Dashboard calendar project.

## Context
- VPS: 72.60.218.33 (srv1065721.hstgr.cloud)
- User: root
- SSH Key: ~/.ssh/id_ed25519
- Domain: life.prabhanshu.space

## What to do

1. **Check existing setup** - Read ~/Programs/vps_bootstrap/ to understand current deployment pattern

2. **Add DNS record** for life.prabhanshu.space → 72.60.218.33
   - Check how DNS is managed (Cloudflare? Hostinger?)

3. **Create nginx config** for life.prabhanshu.space
   - Reverse proxy to localhost:8081 (where Pi tunnel will connect)
   - SSL via certbot

4. **Deploy** via the existing CI/CD pattern (don't manually change VPS)

5. **Update the task file** at:
   ~/Programs/life-dashboard/vps-setup/TASK.md

   Mark completed tasks with [x] and add status updates at the bottom.

## Important Rules
- Follow ~/Programs/CLAUDE.md VPS Access Rules
- No manual changes on VPS - use GitHub Actions
- Read-only diagnostics on VPS are OK

## Verification
After deployment, run:
```bash
curl -sI https://life.prabhanshu.space
```
Should return 502 (expected - Pi tunnel not connected yet).

## When Done
Update TASK.md with completion status so the main agent can verify.
```

---

## How to Use

1. Open a new terminal
2. Navigate to ~/Programs
3. Run: `claude`
4. Paste the prompt above
5. Let the agent complete the VPS setup
6. Check ~/Programs/life-dashboard/vps-setup/TASK.md for completion status
