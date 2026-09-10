# musix

# Pi Music Visualizer

Turn a Raspberry Pi 5 + projector into an AirPlay speaker that shows a live
spectrum visualizer and "now playing" info for whatever you're playing from
Apple Music (or any app) on your iPhone/Mac.

Just two files: `visualizer.py` (the whole app) and `setup.sh` (installs
and configures everything, and makes it start automatically on boot).

## How it works

Apple Music has no official Linux app or SDK, so this doesn't try to run
Apple Music *on* the Pi. Instead:

1. The Pi advertises itself as an **AirPlay receiver** using `shairport-sync`.
2. You open Apple Music on your iPhone/Mac, tap AirPlay, and pick the Pi.
3. Audio streams to the Pi. `shairport-sync` also emits song metadata
   (title/artist/album/artwork/play state) to a named pipe.
4. `visualizer.py` reads that metadata pipe **and** taps the raw audio (via
   an ALSA loopback device) to compute a live spectrum, then renders it all
   fullscreen — out the HDMI port to your projector.
5. A background service copies the audio from the loopback tap to the
   projector's real HDMI audio output, so you actually hear it too.

```
iPhone/Mac (Apple Music)
      │  AirPlay
      ▼
shairport-sync ──► metadata pipe ──┐
      │                            ├──► visualizer.py (fullscreen)
      ▼                            │
ALSA Loopback ──► FFT bars ────────┘
      │
      ▼
alsaloop ──► real HDMI audio out (projector)
```

## Setup

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager (enable
   SSH in the imager settings). Desktop also works, but Lite is leaner
   since the app renders straight to the display without needing a
   desktop environment.
2. Clone this repo onto the Pi:
   ```bash
   git clone <https://github.com/lionfk26/musix> pi-music-visualizer
   cd pi-music-visualizer
   ```
3. Run the setup script:
   ```bash
   sudo ./setup.sh
   ```
   This installs `shairport-sync`, wires up ALSA, creates a Python virtual
   environment, installs dependencies, auto-detects your projector's HDMI
   audio device, and enables everything to start on boot.
4. Reboot to confirm autostart works:
   ```bash
   sudo reboot
   ```

After it comes back up, the projector shows an idle "Waiting for
AirPlay…" screen. Open Apple Music on your phone, tap AirPlay, choose the
Pi ("Living Room Visualizer" by default), and hit play.

## Autostart

`setup.sh` enables two systemd services so everything comes up on its own
after every boot/power cycle — no need to SSH in and run anything manually:

- `music-visualizer` — the fullscreen app
- `hdmi-forward` — routes audio to your projector's speakers

Useful commands:
```bash
sudo systemctl status music-visualizer     # check it's running
sudo systemctl restart music-visualizer    # apply changes after editing visualizer.py
journalctl -u music-visualizer -f          # live logs
```

## Customizing

All the knobs are constants at the top of `visualizer.py` (section `1. CONFIG`):

- `AIRPLAY_NAME` — what shows up in the AirPlay picker (also update
  `AIRPLAY_NAME` inside `setup.sh` if you change this, then re-run setup)
- `NUM_BARS`, `BAR_COLOR_TOP` / `BAR_COLOR_BOTTOM` — spectrum bar look
- `FPS` — render frame rate
- `SHOW_ALBUM_ART_BACKGROUND` — blurred album art as the backdrop

After editing, just restart the service — no need to re-run `setup.sh`:
```bash
sudo systemctl restart music-visualizer
```

## Troubleshooting

- **No sound, visualizer works fine**: `setup.sh` auto-detects your HDMI
  audio card, but double-check it guessed right: compare
  `/etc/systemd/system/hdmi-forward.service` against `aplay -l`, edit if
  needed, then `sudo systemctl restart hdmi-forward`.
- **Pi doesn't show up in AirPlay list**: confirm `avahi-daemon` is running
  (`systemctl status avahi-daemon`) and the Pi is on the same
  network/subnet as your phone (some guest Wi-Fi networks block this).
- **Visualizer screen is blank/black**: you're likely on Pi OS Desktop —
  either switch to Lite, or edit `/etc/systemd/system/music-visualizer.service`
  to remove `SDL_VIDEODRIVER=kmsdrm` and set `Environment=DISPLAY=:0`
  instead, with the service running `After=graphical.target`.
- **Choppy spectrum bars**: lower `FPS` in `visualizer.py`, or check
  `journalctl -u music-visualizer -f` for dropped-frame warnings.