#!/bin/bash
# VPS deployment script for Life Dashboard Calendar
# Run on VPS via GitHub Actions
set -e

DOMAIN="calendar.prabhanshu.space"
EMAIL="mail.prabhanshu@gmail.com"
REPO_DIR="/var/www/life-dashboard"
NGINX_CONF="/etc/nginx/sites-available/$DOMAIN"
NGINX_ENABLED="/etc/nginx/sites-enabled/$DOMAIN"

echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] Starting deployment for $DOMAIN"

# 1. Copy nginx config
echo "[INFO] Copying nginx config..."
if [ -f "$NGINX_CONF" ]; then
    cp "$NGINX_CONF" "$NGINX_CONF.bak.$(date +%Y%m%d_%H%M%S)"
    echo "[INFO] Backed up existing config"
fi
cp "$REPO_DIR/deploy/nginx/$DOMAIN.conf" "$NGINX_CONF"

# 2. Enable site (create symlink if not exists)
if [ ! -L "$NGINX_ENABLED" ]; then
    ln -sf "$NGINX_CONF" "$NGINX_ENABLED"
    echo "[INFO] Enabled site in nginx"
fi

# 3. Test nginx config
echo "[INFO] Testing nginx config..."
nginx -t

# 4. SSL certificate management (using certbot --reinstall pattern)
echo "[INFO] Managing SSL certificate..."
if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    echo "[INFO] Obtaining new SSL certificate..."
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect
else
    echo "[INFO] Reinstalling SSL config (cert exists)..."
    certbot --nginx -d "$DOMAIN" --reinstall --redirect --non-interactive
fi

# 5. Reload nginx
echo "[INFO] Reloading nginx..."
systemctl reload nginx

# 6. Verify deployment
echo "[INFO] Verifying deployment..."
sleep 2

# Check nginx is running
if systemctl is-active --quiet nginx; then
    echo "[OK] nginx is running"
else
    echo "[ERROR] nginx is not running!"
    exit 1
fi

# Check HTTPS responds (expect 502 until Pi tunnel connects)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN/health" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    echo "[OK] Health check passed - tunnel is connected!"
elif [ "$HTTP_CODE" = "502" ]; then
    echo "[OK] Health check returned 502 - expected (Pi tunnel not yet connected)"
elif [ "$HTTP_CODE" = "000" ]; then
    echo "[WARN] Could not reach https://$DOMAIN - DNS may not be configured yet"
else
    echo "[WARN] Health check returned HTTP $HTTP_CODE"
fi

echo ""
echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] Deployment complete!"
echo ""
echo "Next steps:"
echo "  1. Ensure DNS A record: $DOMAIN -> 72.60.218.33"
echo "  2. Connect Pi tunnel to complete the setup"
echo "  3. Test: curl https://$DOMAIN/api/health"
