#!/bin/bash
# Life Dashboard - Raspberry Pi Zero 2 W Bootstrap Script
#
# Architecture: Pi -> SSH Tunnel -> VPS -> nginx reverse proxy -> Internet
# No Tailscale needed - uses persistent SSH tunnel with autossh
#
# Prerequisites:
# - Raspberry Pi Zero 2 W with Raspberry Pi OS Lite (64-bit)
# - WiFi configured (wpa_supplicant.conf in boot partition)
# - SSH enabled (empty 'ssh' file in boot partition)
# - VPS at 72.60.218.33 with SSH access
#
# Usage:
#   scp this script to Pi, then:
#   chmod +x setup-pi.sh && ./setup-pi.sh

set -e

VPS_HOST="72.60.218.33"
VPS_USER="root"
REMOTE_PORT="8081"  # Port on VPS that forwards to Pi's 8080
LOCAL_PORT="8080"   # Calendar API runs on this port

echo "=== Life Dashboard Pi Zero 2 W Setup ==="
echo ""
echo "This will:"
echo "  1. Install Python, autossh, and dependencies"
echo "  2. Generate SSH key for VPS tunnel"
echo "  3. Set up persistent SSH tunnel to VPS"
echo "  4. Install and start calendar service"
echo ""
read -p "Press Enter to continue..."

# Update system
echo "[1/8] Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Install dependencies
echo "[2/8] Installing dependencies..."
sudo apt install -y python3 python3-pip python3-venv sqlite3 git curl autossh

# Generate SSH key for VPS tunnel if it doesn't exist
echo "[3/8] Setting up SSH key for VPS tunnel..."
if [ ! -f ~/.ssh/id_ed25519_vps ]; then
    ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_vps -N "" -C "pi-calendar-tunnel"
    echo ""
    echo ">>> ACTION REQUIRED <<<"
    echo "Add this public key to VPS authorized_keys:"
    echo ""
    cat ~/.ssh/id_ed25519_vps.pub
    echo ""
    echo "On VPS, run:"
    echo "  echo 'PUBLIC_KEY_ABOVE' >> ~/.ssh/authorized_keys"
    echo ""
    read -p "Press Enter after you've added the key to VPS..."
fi

# Test SSH connection to VPS
echo "[4/8] Testing SSH connection to VPS..."
if ssh -i ~/.ssh/id_ed25519_vps -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$VPS_USER@$VPS_HOST" "echo 'VPS connection OK'"; then
    echo "SSH to VPS successful!"
else
    echo "ERROR: Cannot connect to VPS"
    echo "Make sure the SSH key is added to VPS authorized_keys"
    exit 1
fi

# Create app directory
echo "[5/8] Setting up application..."
mkdir -p ~/life-dashboard/templates
cd ~/life-dashboard

# Create Python virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install --upgrade pip
pip install fastapi uvicorn pydantic

# Create systemd service for calendar API
echo "[6/8] Installing calendar API service..."
sudo tee /etc/systemd/system/calendar.service > /dev/null << 'EOF'
[Unit]
Description=Life Dashboard Calendar API
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/life-dashboard
ExecStart=/home/pi/life-dashboard/venv/bin/python -m uvicorn calendar_api:app --host 127.0.0.1 --port 8080
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# Create systemd service for SSH tunnel (autossh)
echo "[7/8] Installing SSH tunnel service..."
sudo tee /etc/systemd/system/calendar-tunnel.service > /dev/null << EOF
[Unit]
Description=Life Dashboard SSH Tunnel to VPS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Environment="AUTOSSH_GATETIME=0"
ExecStart=/usr/bin/autossh -M 0 -N -o "ServerAliveInterval=30" -o "ServerAliveCountMax=3" -o "ExitOnForwardFailure=yes" -o "StrictHostKeyChecking=accept-new" -i /home/pi/.ssh/id_ed25519_vps -R ${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT} ${VPS_USER}@${VPS_HOST}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Enable and start services
sudo systemctl daemon-reload
sudo systemctl enable calendar calendar-tunnel
# Don't start yet - files need to be deployed first

echo "[8/8] Setup complete!"
echo ""
echo "=== Next Steps ==="
echo ""
echo "1. Deploy application files:"
echo "   From your laptop, run: ./scripts/deploy.sh"
echo ""
echo "2. VPS nginx configuration needed:"
echo "   Add reverse proxy config for life.prabhanshu.space"
echo "   pointing to localhost:$REMOTE_PORT"
echo ""
echo "3. After deployment, services will start automatically"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status calendar           # API status"
echo "  sudo systemctl status calendar-tunnel    # Tunnel status"
echo "  sudo journalctl -u calendar -f           # API logs"
echo "  sudo journalctl -u calendar-tunnel -f    # Tunnel logs"
