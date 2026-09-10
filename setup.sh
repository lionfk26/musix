#!/usr/bin/env bash
# Sets up pi-music-visualizer end-to-end on a Raspberry Pi 5 and makes it
# start automatically on every boot.
#
# Usage:
#   sudo ./setup.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo ./setup.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/pi-music-visualizer"
RUN_AS_USER="${SUDO_USER:-pi}"
AIRPLAY_NAME="Living Room Visualizer"   # keep in sync with visualizer.py's AIRPLAY_NAME

echo "==> [1/8] Installing system packages"
apt-get update
apt-get install -y \
  shairport-sync \
  avahi-daemon \
  libasound2-plugins \
  alsa-utils \
  python3-venv \
  python3-pip \
  fonts-dejavu

echo "==> [2/8] Checking shairport-sync has metadata support"
if ! shairport-sync -V | grep -qi "metadata"; then
  echo "WARNING: your shairport-sync build may not include metadata support."
  echo "Song title/artist/artwork may not display. See:"
  echo "https://github.com/mikebrady/shairport-sync/blob/master/BUILD.md"
fi

echo "==> [3/8] Enabling ALSA loopback kernel module"
if ! grep -q "^snd-aloop" /etc/modules 2>/dev/null; then
  echo "snd-aloop" >> /etc/modules
fi
modprobe snd-aloop || true

echo "==> [4/8] Writing /etc/asound.conf"
[[ -f /etc/asound.conf ]] && cp /etc/asound.conf "/etc/asound.conf.bak.$(date +%s)"
cat > /etc/asound.conf <<'EOF'
# snd-aloop creates a pair of virtual devices: what's written to
# hw:Loopback,0,0 (playback) can be read back from hw:Loopback,1,0 (capture).
# This dsnoop device lets MULTIPLE processes (the HDMI forwarder and the
# visualizer) read that capture side at the same time.

pcm.loopback_in {
    type dsnoop
    ipc_key 555555
    ipc_key_add_uid false
    slave {
        pcm "hw:Loopback,1,0"
        channels 2
        rate 44100
        format S16_LE
    }
}

ctl.loopback_in {
    type hw
    card Loopback
}
EOF

echo "==> [5/8] Writing /etc/shairport-sync.conf"
[[ -f /etc/shairport-sync.conf ]] && cp /etc/shairport-sync.conf "/etc/shairport-sync.conf.bak.$(date +%s)"
cat > /etc/shairport-sync.conf <<EOF
general = {
  name = "${AIRPLAY_NAME}";
  port = 5000;
};

metadata = {
  enabled = "yes";
  include_cover_art = "yes";
  cover_art_cache_directory = "/tmp/shairport-sync/.cache/coverart";
  pipe_name = "/tmp/shairport-sync-metadata";
  pipe_timeout = 5000;
};

alsa = {
  output_device = "hw:Loopback,0,0";
  mixer_control_name = "PCM";
};
EOF

echo "==> [6/8] Installing app + Python virtual environment"
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/visualizer.py" "$INSTALL_DIR/"
chown -R "$RUN_AS_USER":"$RUN_AS_USER" "$INSTALL_DIR"
usermod -aG video,render,input "$RUN_AS_USER" || true

sudo -u "$RUN_AS_USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$RUN_AS_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$RUN_AS_USER" "$INSTALL_DIR/venv/bin/pip" install pygame numpy sounddevice

echo "==> [7/8] Detecting HDMI audio card and writing systemd services"
HDMI_CARD="$(aplay -l 2>/dev/null | grep -i vc4hdmi | head -1 | sed -n 's/^card [0-9]*: \([A-Za-z0-9_]*\).*/\1/p')"
if [[ -z "$HDMI_CARD" ]]; then
  echo "Could not auto-detect an HDMI audio card. Defaulting to 'vc4hdmi0' â"
  echo "if that's wrong, edit /etc/systemd/system/hdmi-forward.service and"
  echo "run: sudo systemctl restart hdmi-forward"
  HDMI_CARD="vc4hdmi0"
else
  echo "Detected HDMI audio card: hw:$HDMI_CARD"
fi

cat > /etc/systemd/system/hdmi-forward.service <<EOF
[Unit]
Description=Forward ALSA loopback audio to real HDMI output
After=sound.target
Requires=sound.target

[Service]
ExecStart=/usr/bin/alsaloop -C loopback_in -P hw:${HDMI_CARD} -t 100000
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/music-visualizer.service <<EOF
[Unit]
Description=Apple Music AirPlay Visualizer
After=sound.target network-online.target shairport-sync.service hdmi-forward.service
Wants=shairport-sync.service hdmi-forward.service

[Service]
Environment=SDL_VIDEODRIVER=kmsdrm
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/visualizer.py
Restart=always
RestartSec=3
User=${RUN_AS_USER}
SupplementaryGroups=video render input

[Install]
WantedBy=multi-user.target
EOF

echo "==> [8/8] Enabling everything to start on boot"
systemctl daemon-reload
systemctl enable --now avahi-daemon
systemctl restart shairport-sync
systemctl enable --now hdmi-forward
systemctl enable --now music-visualizer

echo ""
echo "==> Done! AirPlay name: \"${AIRPLAY_NAME}\""
echo "The visualizer is running now and will auto-start on every boot."
echo ""
echo "If you don't hear audio, double-check the detected HDMI card was"
echo "correct (compare with: aplay -l), then:"
echo "  sudo nano /etc/systemd/system/hdmi-forward.service"
echo "  sudo systemctl restart hdmi-forward"
echo ""
echo "Reboot to verify the full autostart flow: sudo reboot"
