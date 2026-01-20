# VPS Setup Task for Life Dashboard

## Status: IN PROGRESS (CI/CD Ready - Awaiting DNS)

**Last Updated**: 2026-01-20 09:26 IST
**Agent**: VPS Setup Agent

---

## Objective

Configure VPS (72.60.218.33) to act as a reverse proxy for the Life Dashboard calendar running on a Raspberry Pi Zero 2 W at home.

## Architecture

```
Pi Zero 2 W (home network)
    │
    └── autossh reverse tunnel ──► VPS:8082
                                      │
                                      └── nginx ──► life.prabhanshu.space
```

The Pi initiates an outbound SSH tunnel to the VPS. The VPS nginx proxies HTTPS traffic to that tunnel.

## Tasks

### 1. DNS Record
- [ ] Add A record: `life.prabhanshu.space` → `72.60.218.33`
- Provider: Check existing DNS setup for prabhanshu.space

### 2. nginx Configuration
Create `/etc/nginx/sites-available/life.prabhanshu.space`:

```nginx
server {
    listen 80;
    server_name life.prabhanshu.space;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name life.prabhanshu.space;

    # SSL configured by certbot

    location / {
        proxy_pass http://127.0.0.1:8082;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
```

### 3. SSL Certificate
```bash
certbot --nginx -d life.prabhanshu.space --non-interactive --agree-tos -m mail.prabhanshu@gmail.com
```

### 4. SSH Key for Pi Tunnel
The Pi will connect to VPS using a dedicated SSH key. Add this to `/root/.ssh/authorized_keys`:
```
# Pi Calendar Tunnel - to be added after Pi generates key
# Format: ssh-ed25519 AAAA... pi-calendar-tunnel
```

### 5. Firewall (if UFW enabled)
```bash
# Port 8082 only needs localhost access (nginx -> tunnel)
# No external firewall rule needed
```

---

## Verification

After setup, these should work:

1. **DNS resolves**:
   ```bash
   dig +short life.prabhanshu.space
   # Expected: 72.60.218.33
   ```

2. **nginx config valid**:
   ```bash
   nginx -t
   ```

3. **HTTPS works** (will show 502 until Pi tunnel is up):
   ```bash
   curl -sI https://life.prabhanshu.space
   # Expected: HTTP/2 502 (tunnel not yet connected)
   ```

4. **Port 8082 listening** (after Pi connects):
   ```bash
   ss -tlnp | grep 8082
   ```

---

## Completion Checklist

Update this section when done:

- [ ] DNS record created (USER ACTION: Hostinger dashboard)
- [x] nginx config created (`deploy/nginx/life.prabhanshu.space.conf`)
- [x] Deploy script created (`deploy/vps-deploy.sh`)
- [x] GitHub Actions workflow created (`.github/workflows/deploy-vps.yml`)
- [x] GitHub secrets configured (SSH_PRIVATE_KEY, VPS_HOST, VPS_USERNAME)
- [ ] Push to GitHub to trigger deployment
- [ ] nginx deployed to VPS (automated via CI/CD)
- [ ] SSL certificate obtained (automated via certbot)
- [ ] Verified with curl

## Status Updates

```
[2026-01-20 09:26] VPS Agent started
[2026-01-20 09:26] Created nginx config at deploy/nginx/life.prabhanshu.space.conf
[2026-01-20 09:26] Created deploy script at deploy/vps-deploy.sh
[2026-01-20 09:26] Created GitHub Actions workflow at .github/workflows/deploy-vps.yml
[2026-01-20 09:26] Set GitHub secrets: SSH_PRIVATE_KEY, VPS_HOST, VPS_USERNAME
[2026-01-20 09:26] WAITING: DNS A record needs to be added via Hostinger dashboard
```

---

## DNS Setup Instructions (USER ACTION REQUIRED)

**Provider**: Hostinger (same as prabhanshu.space)

1. Log into Hostinger DNS management for prabhanshu.space
2. Add A record:
   - **Type**: A
   - **Name**: calendar
   - **Points to**: 72.60.218.33
   - **TTL**: 3600 (or default)
3. Wait 5-10 minutes for DNS propagation
4. Verify: `getent hosts life.prabhanshu.space` should return 72.60.218.33

---

## Files Created by VPS Agent

| File | Purpose |
|------|---------|
| `deploy/nginx/life.prabhanshu.space.conf` | nginx reverse proxy config |
| `deploy/vps-deploy.sh` | VPS deployment script (copies config, runs certbot) |
| `.github/workflows/deploy-vps.yml` | CI/CD pipeline for VPS deployment |

---

## Notes

- This setup follows the VPS Access Rules from CLAUDE.md - all changes via GitHub Actions
- Deployment is triggered on push to main branch (paths: deploy/**)
- Can also be triggered manually via GitHub Actions "Run workflow"
- SSL will be obtained automatically via certbot --reinstall pattern
