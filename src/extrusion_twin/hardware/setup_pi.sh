#!/usr/bin/env bash
# setup_pi.sh -- provision a Raspberry Pi in the panel to run the twin console.
#
#   sudo bash setup_pi.sh --ssid PANEL-LINK --pass 'long-random-passphrase' [--ap-fallback]
#
# What it does, all of it reversible:
#   * installs python3, numpy, pyserial and minimalmodbus from apt (no compiling)
#   * adds a wpa_supplicant network block for the named hotspot, so the Pi joins whenever
#     that network appears with the right passphrase
#   * installs avahi-daemon so the Pi answers to extrusion-twin.local
#   * optionally raises its own access point when no known network has been seen for a while,
#     so you can always reach it
#   * installs a systemd service that starts the console on boot with an access key
#
# It does not touch the RS-485 side. It does not enable any control path; the twin only reads.
set -euo pipefail

SSID=""; PASS=""; AP_FALLBACK=0; PORT=8000
USERNAME="${SUDO_USER:-pi}"
HOME_DIR="$(getent passwd "$USERNAME" | cut -d: -f6)"
APP_DIR="$HOME_DIR/extrusion_twin"
AP_SSID="EXTRUSION-PANEL"; AP_PASS="extrusion-panel"; AP_IP="192.168.50.1"

usage(){ sed -n '2,16p' "$0"; exit 1; }
while [[ $# -gt 0 ]]; do case "$1" in
  --ssid) SSID="$2"; shift 2;;
  --pass) PASS="$2"; shift 2;;
  --port) PORT="$2"; shift 2;;
  --ap-fallback) AP_FALLBACK=1; shift;;
  --ap-ssid) AP_SSID="$2"; shift 2;;
  --ap-pass) AP_PASS="$2"; shift 2;;
  -h|--help) usage;;
  *) echo "unknown option $1"; usage;;
esac; done
[[ -n "$SSID" && -n "$PASS" ]] || { echo "need --ssid and --pass"; usage; }
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
[[ ${#PASS} -ge 8 ]] || { echo "a WPA2 passphrase must be at least 8 characters"; exit 1; }

say(){ printf '\n== %s\n' "$*"; }

# ---------------------------------------------------------------- packages
say "Installing packages"
apt-get update -qq
apt-get install -y -qq python3 python3-numpy python3-serial avahi-daemon >/dev/null
apt-get install -y -qq python3-minimalmodbus >/dev/null 2>&1 || \
  pip3 install --break-system-packages -q minimalmodbus || \
  echo "  note: install minimalmodbus by hand if you plan to use a serial (not network) adapter"

# ---------------------------------------------------------------- the twin
say "Placing the twin in $APP_DIR"
mkdir -p "$APP_DIR"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"       # the extrusion_twin package folder
if [[ -f "$SRC/run.py" ]]; then
  mkdir -p "$APP_DIR/extrusion_twin"
  cp -r "$SRC"/*.py "$SRC"/dashboard.html "$APP_DIR/extrusion_twin/"
  cp -r "$SRC/hardware" "$APP_DIR/extrusion_twin/" 2>/dev/null || true
else
  echo "  could not find the package next to this script; copy the extrusion_twin folder into $APP_DIR yourself"
fi
chown -R "$USERNAME:$USERNAME" "$APP_DIR"

# the serial port needs group access, not root
usermod -aG dialout "$USERNAME" || true

# ---------------------------------------------------------------- wifi
say "Setting the Pi to join '$SSID' whenever it appears"
WPA=/etc/wpa_supplicant/wpa_supplicant.conf
touch "$WPA"; chmod 600 "$WPA"
grep -q "^country=" "$WPA" || echo "country=US" >> "$WPA"
grep -q "^ctrl_interface=" "$WPA" || echo "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev" >> "$WPA"
grep -q "^update_config=" "$WPA" || echo "update_config=1" >> "$WPA"
if grep -q "ssid=\"$SSID\"" "$WPA"; then
  echo "  a block for '$SSID' is already there; leaving it alone"
else
  # priority 10 so an authorized hotspot wins over any bench network also configured
  {
    echo ""
    echo "network={"
    echo "    ssid=\"$SSID\""
    echo "    psk=\"$PASS\""
    echo "    key_mgmt=WPA-PSK"
    echo "    priority=10"
    echo "}"
  } >> "$WPA"
fi
rfkill unblock wifi 2>/dev/null || true

# ---------------------------------------------------------------- mDNS name
say "Naming the Pi extrusion-twin.local"
hostnamectl set-hostname extrusion_twin 2>/dev/null || echo extrusion_twin > /etc/hostname
grep -q "127.0.1.1.*extrusion_twin" /etc/hosts || echo "127.0.1.1	extrusion_twin" >> /etc/hosts
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

# ---------------------------------------------------------------- access point fallback
if [[ $AP_FALLBACK -eq 1 ]]; then
  say "Setting up the fallback access point '$AP_SSID' at $AP_IP"
  apt-get install -y -qq hostapd dnsmasq >/dev/null
  systemctl unmask hostapd >/dev/null 2>&1 || true
  systemctl disable --now hostapd dnsmasq >/dev/null 2>&1 || true   # started only by the watcher below

  cat > /etc/hostapd/twin-ap.conf <<EOF
interface=wlan0
driver=nl80211
ssid=$AP_SSID
hw_mode=g
channel=6
wmm_enabled=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$AP_PASS
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF
  chmod 600 /etc/hostapd/twin-ap.conf

  cat > /etc/dnsmasq.d/twin-ap.conf <<EOF
interface=wlan0
dhcp-range=${AP_IP%.*}.10,${AP_IP%.*}.60,255.255.255.0,12h
EOF

  cat > /usr/local/bin/twin-netwatch <<EOF
#!/usr/bin/env bash
# Join a known network if one is there. If none has appeared for three minutes, raise our own
# access point so the console is always reachable, and keep checking in case one comes back.
GRACE=180
while true; do
  if iw dev wlan0 link 2>/dev/null | grep -q "Connected to"; then
    if systemctl is-active --quiet hostapd; then
      systemctl stop hostapd dnsmasq
      ip addr flush dev wlan0
    fi
    sleep 20; continue
  fi
  waited=0
  while (( waited < GRACE )); do
    sleep 10; waited=\$((waited+10))
    iw dev wlan0 link 2>/dev/null | grep -q "Connected to" && break
  done
  if ! iw dev wlan0 link 2>/dev/null | grep -q "Connected to"; then
    if ! systemctl is-active --quiet hostapd; then
      ip addr flush dev wlan0
      ip addr add $AP_IP/24 dev wlan0
      systemctl start dnsmasq
      hostapd -B /etc/hostapd/twin-ap.conf 2>/dev/null || systemctl start hostapd
    fi
    # give an authorized hotspot a chance to appear and win
    sleep 120
    if iw dev wlan0 scan 2>/dev/null | grep -q "SSID: $SSID"; then
      systemctl stop hostapd dnsmasq 2>/dev/null; pkill hostapd 2>/dev/null
      ip addr flush dev wlan0
      wpa_cli -i wlan0 reconfigure >/dev/null 2>&1
    fi
  fi
done
EOF
  chmod +x /usr/local/bin/twin-netwatch

  cat > /etc/systemd/system/twin-netwatch.service <<EOF
[Unit]
Description=Panel network watcher: join the authorized hotspot, or raise our own
After=network.target

[Service]
ExecStart=/usr/local/bin/twin-netwatch
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now twin-netwatch >/dev/null
fi

# ---------------------------------------------------------------- the console service
say "Installing the console service"
cat > /etc/systemd/system/extrusion-twin.service <<EOF
[Unit]
Description=Extrusion line melt-state console
After=network.target

[Service]
Type=simple
User=$USERNAME
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 -m extrusion_twin serve --host 0.0.0.0 --port $PORT --token --no-browser --dir $APP_DIR/twin_data
Restart=always
RestartSec=5
StandardOutput=append:$APP_DIR/console.log
StandardError=append:$APP_DIR/console.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now extrusion-twin >/dev/null
sleep 3

TOKEN_FILE="$APP_DIR/twin_data/console_token.txt"
say "Done"
echo "  Network:  the Pi will join '$SSID' whenever a hotspot with that name and passphrase appears."
[[ $AP_FALLBACK -eq 1 ]] && echo "  Fallback: if none appears for three minutes it raises '$AP_SSID' at http://$AP_IP:$PORT/"
echo "  Address:  http://extrusion-twin.local:$PORT/  (or the address the hotspot gave it)"
if [[ -f "$TOKEN_FILE" ]]; then
  echo "  Key:      $(cat "$TOKEN_FILE")"
  echo "            open  http://extrusion-twin.local:$PORT/?token=$(cat "$TOKEN_FILE")"
  echo "            the browser keeps it after the first visit, so it is pasted once per device"
else
  echo "  Key:      will be in $TOKEN_FILE once the service has started; check 'systemctl status extrusion-twin'"
fi
echo
echo "  Log:      $APP_DIR/console.log"
echo "  Restart:  sudo systemctl restart extrusion-twin"
echo "  Check:    cd $APP_DIR && python3 -m extrusion_twin.tests"
echo
echo "  A reboot is worth doing now, to confirm it all comes back on its own."
