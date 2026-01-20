# Life Dashboard - Complete Setup Plan

## Current Status

| Component | Status |
|-----------|--------|
| Calendar API code | ✅ Complete |
| Dashboard HTML | ✅ Complete |
| Claude /calendar skill | ✅ Complete |
| Pi setup scripts | ✅ Complete |
| VPS nginx setup | ⏳ Pending (parallel agent) |
| Pi OS flashing | ⏳ Your action needed |
| Pi deployment | ⏳ Waiting for Pi boot |

---

## Pi Hardware Issue - READ THIS FIRST

### Why No LED?
The Raspberry Pi Zero 2 W shows **no LED** because:
1. **No OS on SD card** - The Pi needs Raspberry Pi OS flashed to the SD card
2. **Not enough power** - Laptop USB ports often can't supply 5V/1.2A needed

### Pi Zero 2 W Ports
```
┌─────────────────────────────────────────┐
│  [mini HDMI]  [USB]  [PWR IN]  [camera] │
│               data    power              │
└─────────────────────────────────────────┘
```
- **PWR IN** (right): Power only - use a proper 5V/2A adapter, not laptop USB
- **USB** (left): Data/OTG - only use after Pi is booted for connecting devices

### Your 128GB SD Card
Great size! Now it needs to be flashed with Raspberry Pi OS.

---

## Step 1: Flash Raspberry Pi OS (YOUR ACTION)

### Option A: Using Raspberry Pi Imager (Recommended)

1. **Download** Raspberry Pi Imager:
   - https://www.raspberrypi.com/software/
   - Or: `yay -S rpi-imager`

2. **Insert SD card** into laptop (via USB adapter if needed)

3. **Run Imager** and configure:
   - OS: **Raspberry Pi OS Lite (64-bit)** - no desktop needed
   - Storage: Your 128GB SD card
   - Click ⚙️ **Settings** before writing:
     - ✅ Set hostname: `pi-calendar`
     - ✅ Enable SSH (password authentication)
     - ✅ Set username/password: `pi` / (choose password)
     - ✅ Configure WiFi: Your home SSID and password
     - ✅ Set locale: Your timezone

4. **Write** to SD card (takes ~5 min)

### Option B: Manual (if Imager doesn't work)

```bash
# 1. Download image
wget https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2024-11-19/2024-11-19-raspios-bookworm-arm64-lite.img.xz

# 2. Find SD card device
lsblk  # Look for your 128GB device, e.g., /dev/sdb

# 3. Flash (CAREFUL - double check device!)
xzcat 2024-11-19-raspios-bookworm-arm64-lite.img.xz | sudo dd of=/dev/sdX bs=4M status=progress

# 4. Mount boot partition and configure
sudo mount /dev/sdX1 /mnt
sudo touch /mnt/ssh  # Enable SSH

# 5. Create WiFi config
sudo tee /mnt/wpa_supplicant.conf << EOF
country=IN
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={
    ssid="YOUR_WIFI_SSID"
    psk="YOUR_WIFI_PASSWORD"
    key_mgmt=WPA-PSK
}
EOF

sudo umount /mnt
```

---

## Step 2: Boot Pi and Find IP (YOUR ACTION)

1. **Insert SD card** into Pi Zero 2 W
2. **Connect power** using a proper 5V/2A USB power adapter (NOT laptop USB)
3. **Wait 2-3 minutes** for first boot (green LED will blink)
4. **Find Pi's IP**:
   ```bash
   # Option 1: Try mDNS
   ping pi-calendar.local

   # Option 2: Scan network
   nmap -sn 192.168.1.0/24 | grep -B2 "Raspberry"

   # Option 3: Check router's DHCP client list
   ```

5. **Test SSH**:
   ```bash
   ssh pi@<PI_IP>
   # Enter password you set in Imager
   ```

---

## Step 3: VPS Setup (PARALLEL - Other Agent)

While waiting for Pi to boot, start another Claude Code session for VPS setup.

### Start VPS Agent

```bash
# In a new terminal:
cd ~/Programs
claude
```

### Paste this prompt:

```
I need you to set up nginx reverse proxy on my VPS for the Life Dashboard calendar project.

## Context
- VPS: 72.60.218.33 (srv1065721.hstgr.cloud)
- User: root
- SSH Key: ~/.ssh/id_ed25519
- Domain: calendar.prabhanshu.space

## What to do

1. Check existing setup - Read ~/Programs/vps_bootstrap/ to understand current deployment pattern

2. Add DNS record for calendar.prabhanshu.space → 72.60.218.33

3. Create nginx config for calendar.prabhanshu.space
   - Reverse proxy to localhost:8081 (where Pi tunnel will connect)
   - SSL via certbot

4. Deploy via the existing CI/CD pattern (don't manually change VPS)

5. Update the task file at ~/Programs/life-dashboard/vps-setup/TASK.md
   Mark completed tasks with [x] and add status updates at the bottom.

## Important Rules
- Follow ~/Programs/CLAUDE.md VPS Access Rules
- No manual changes on VPS - use GitHub Actions
- Read-only diagnostics on VPS are OK

## When Done
Update TASK.md with completion status.
```

---

## Step 4: Pi Bootstrap (AFTER Pi boots)

Once you can SSH into the Pi:

```bash
# From laptop:
cd ~/Programs/life-dashboard

# Copy setup script to Pi
scp scripts/setup-pi.sh pi@<PI_IP>:~/

# SSH in and run setup
ssh pi@<PI_IP>
chmod +x setup-pi.sh
./setup-pi.sh
```

The script will:
1. Install Python, autossh, dependencies
2. Generate SSH key for VPS tunnel
3. Display the public key - **add this to VPS**
4. Set up systemd services

---

## Step 5: Connect Pi to VPS (AFTER both are ready)

1. **Add Pi's SSH key to VPS** (the setup script shows this)
2. **Deploy app to Pi**:
   ```bash
   cd ~/Programs/life-dashboard
   ./scripts/deploy.sh pi@<PI_IP>
   ```
3. **Verify tunnel**:
   ```bash
   ssh pi@<PI_IP> 'sudo systemctl status calendar-tunnel'
   ```

---

## Step 6: Verify Everything

```bash
# 1. Check VPS task completion
cat ~/Programs/life-dashboard/vps-setup/TASK.md

# 2. Test the full chain
curl https://calendar.prabhanshu.space/api/health

# 3. Open dashboard in browser
xdg-open https://calendar.prabhanshu.space

# 4. Test /calendar skill
# In Claude Code: /calendar add test event tomorrow at 3pm
```

---

## Troubleshooting

### Pi won't boot (no LED)
- Use a proper power adapter (5V/2A), not laptop USB
- Re-flash the SD card
- Try a different SD card

### Can't find Pi on network
- Wait longer (first boot is slow)
- Check WiFi credentials in imager settings
- Connect Pi to monitor via mini HDMI to see boot messages

### SSH connection refused
- Verify SSH was enabled (empty `ssh` file in boot partition)
- Check firewall on your network

### Tunnel not connecting
- Check Pi can reach VPS: `ssh -i ~/.ssh/id_ed25519_vps root@72.60.218.33`
- Check VPS has Pi's public key in authorized_keys
- Check logs: `journalctl -u calendar-tunnel -f`

---

## Timeline

| Step | Who | Duration |
|------|-----|----------|
| Flash SD card | You | ~10 min |
| VPS setup | Parallel agent | ~15 min |
| Boot Pi & find IP | You | ~5 min |
| Run Pi setup script | Claude + You | ~10 min |
| Deploy & test | Claude | ~5 min |

**Total**: ~30-45 minutes (with parallel work)
