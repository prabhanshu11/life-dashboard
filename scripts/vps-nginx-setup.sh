#!/bin/bash
# VPS nginx configuration for Life Dashboard
#
# This script is for REFERENCE ONLY - do not run on VPS directly!
# Add this config to your VPS deployment repo and deploy via GitHub Actions
#
# The Pi establishes an SSH tunnel that forwards VPS:8082 -> Pi:8080
# nginx then proxies life.prabhanshu.space -> localhost:8082

cat << 'NGINX_CONFIG'
# /etc/nginx/sites-available/life.prabhanshu.space

server {
    listen 80;
    server_name life.prabhanshu.space;

    # Redirect HTTP to HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name life.prabhanshu.space;

    # SSL will be configured by certbot
    # ssl_certificate /etc/letsencrypt/live/life.prabhanshu.space/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/life.prabhanshu.space/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8082;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support (for future features)
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # Health check endpoint (returns 502 if tunnel is down)
    location /health {
        proxy_pass http://127.0.0.1:8082/api/health;
        proxy_connect_timeout 5s;
        proxy_read_timeout 5s;
    }
}
NGINX_CONFIG

echo ""
echo "=== VPS Setup Instructions ==="
echo ""
echo "1. Add DNS record:"
echo "   life.prabhanshu.space -> 72.60.218.33 (A record)"
echo ""
echo "2. Create nginx config (via vps_bootstrap repo deployment):"
echo "   - Add the config above to vps_bootstrap/nginx/sites/"
echo "   - Push to GitHub to trigger deployment"
echo ""
echo "3. SSL certificate:"
echo "   certbot --nginx -d life.prabhanshu.space"
echo ""
echo "4. Allow Pi's SSH key in /root/.ssh/authorized_keys"
echo "   (The Pi will try to establish a tunnel to port 8082)"
echo ""
echo "5. Optional: Add a watchdog to check tunnel health"
echo "   curl -sf http://127.0.0.1:8082/api/health || echo 'Tunnel down'"
