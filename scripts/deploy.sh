#!/bin/bash
# Deploy Life Dashboard to Raspberry Pi Zero 2 W
#
# Usage: ./scripts/deploy.sh [pi-ip-address]
#
# Default: Uses pi@raspberrypi.local (mDNS)
# With IP: ./scripts/deploy.sh 192.168.1.100

set -e

PI_HOST="${1:-pi@raspberrypi.local}"
REMOTE_DIR="/home/pi/life-dashboard"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)/pi"

echo "=== Deploying Life Dashboard to $PI_HOST ==="
echo ""

# Check if we can reach the Pi
echo "[1/5] Testing connection to Pi..."
if ! ssh -o ConnectTimeout=10 "$PI_HOST" "echo 'Connected'" 2>/dev/null; then
    echo "ERROR: Cannot connect to $PI_HOST"
    echo ""
    echo "If using mDNS (raspberrypi.local) fails, find the Pi's IP:"
    echo "  - Check your router's DHCP clients"
    echo "  - Use: nmap -sn 192.168.1.0/24 | grep -i raspberry"
    echo "  - Connect a display to see IP on boot"
    echo ""
    echo "Then run: ./scripts/deploy.sh pi@<IP_ADDRESS>"
    exit 1
fi

# Create directories on Pi
echo "[2/5] Creating directories..."
ssh "$PI_HOST" "mkdir -p $REMOTE_DIR/templates"

# Copy application files
echo "[3/5] Copying application files..."
scp "$LOCAL_DIR/calendar_api.py" "$PI_HOST:$REMOTE_DIR/"
scp "$LOCAL_DIR/database.py" "$PI_HOST:$REMOTE_DIR/"
scp "$LOCAL_DIR/templates/dashboard.html" "$PI_HOST:$REMOTE_DIR/templates/"

# Start services
echo "[4/5] Starting services..."
ssh "$PI_HOST" "sudo systemctl restart calendar calendar-tunnel"

# Wait and verify
echo "[5/5] Verifying..."
sleep 3

CALENDAR_STATUS=$(ssh "$PI_HOST" "systemctl is-active calendar" 2>/dev/null || echo "failed")
TUNNEL_STATUS=$(ssh "$PI_HOST" "systemctl is-active calendar-tunnel" 2>/dev/null || echo "failed")

echo ""
echo "=== Deployment Status ==="
echo ""
echo "Calendar API:  $CALENDAR_STATUS"
echo "SSH Tunnel:    $TUNNEL_STATUS"
echo ""

if [ "$CALENDAR_STATUS" = "active" ] && [ "$TUNNEL_STATUS" = "active" ]; then
    echo "All services running!"
    echo ""
    echo "Dashboard should be available at:"
    echo "  https://calendar.prabhanshu.space (after VPS nginx setup)"
    echo ""
    echo "Local test on Pi:"
    echo "  ssh $PI_HOST 'curl -s http://127.0.0.1:8080/api/health'"
else
    echo "WARNING: Some services may not be running"
    echo ""
    echo "Debug with:"
    echo "  ssh $PI_HOST 'sudo journalctl -u calendar -n 30'"
    echo "  ssh $PI_HOST 'sudo journalctl -u calendar-tunnel -n 30'"
fi
