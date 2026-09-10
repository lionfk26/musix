#!/usr/bin/env python3
"""
Pi Music Visualizer — single-file version.

Shows a fullscreen "now playing" screen (title/artist/album/artwork) plus a
live spectrum ("sound bars") for whatever is AirPlaying to this Pi via
shairport-sync. Run by setup.sh as a systemd service so it starts on boot.

Sections in this file:
  1. CONFIG          — tweak and restart the service to apply
  2. METADATA READER — tails shairport-sync's metadata pipe
  3. AUDIO ANALYZER   — captures PCM audio + computes FFT bar levels
  4. VISUALIZER       — Pygame fullscreen rendering
  5. MAIN             — wires it all together
"""

import base64
import binascii
import io
import re
import sys
import threading
import time

import numpy as np
import pygame
import sounddevice as sd

# ============================================================
# 1. CONFIG
# ============================================================

# AirPlay / metadata
AIRPLAY_NAME = "Living Room Visualizer"  # keep in sync with shairport-sync.conf
METADATA_PIPE = "/tmp/shairport-sync-metadata"

# Audio capture
AUDIO_DEVICE = "loopback_in"   # matches /etc/asound.conf, written by setup.sh
SAMPLE_RATE = 44100
CHANNELS = 2
BLOCK_SIZE = 1024              # samples per audio callback; lower = snappier, more CPU

# Spectrum bars
NUM_BARS = 32
BAR_SMOOTHING_ATTACK = 0.6     # 0-1, how fast bars rise
BAR_SMOOTHING_DECAY = 0.15     # 0-1, how fast bars fall
BAR_COLOR_TOP = (0, 220, 255)
BAR_COLOR_BOTTOM = (255, 0, 200)
BAR_GAP_PX = 4

# Display
FPS = 30
FULLSCREEN = True
WINDOW_SIZE = (1280, 720)      # used only if FULLSCREEN is False
SHOW_ALBUM_ART_BACKGROUND = True
BACKGROUND_DIM = 0.55          # 0 = fully dark album art, 1 = full brightness
FONT_NAME = "DejaVuSans"

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GRAY = (170, 170, 170)


# ============================================================
# 2. METADATA READER
# ============================================================
#
# shairport-sync writes plain-text blocks to a named pipe like:
#
#     <item>
#     <type>636f7265</type>
#     <code>6173616c</code>
#     <length>10</length>
#     <data encoding="base64">
#     QWxidW0gTmFtZQ==
#     </data>
#     </item>
#
# `type` and `code` are ASCII-hex-encoded 4-character codes.

_ITEM_RE = re.compile(
    r"<item>\s*"
    r"<type>(?P<type>[0-9a-fA-F]+)</type>\s*"
    r"<code>(?P<code>[0-9a-fA-F]+)</code>\s*"
    r"<length>(?P<length>\d+)</length>\s*"
    r"(?:<data encoding=\"base64\">\s*(?P<data>[^<]*)\s*</data>)?\s*"
    r"</item>",
    re.DOTALL,
)


def _hex_to_ascii(hex_str: str) -> str:
    try:
        return binascii.unhexlify(hex_str).decode("ascii", errors="replace")
    except (binascii.Error, UnicodeDecodeError):
        return ""


class NowPlaying:
    """Thread-safe snapshot of current metadata."""

    def __init__(self):
        self._lock = threading.Lock()
        self.title = ""
        self.artist = ""
        self.album = ""
        self.cover_art = None  # raw bytes (jpeg/png) or None
        self.is_playing = False
        self.updated_at = 0.0

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)
            self.updated_at = time.time()

    def snapshot(self):
        with self._lock:
            return {
                "title": self.title,
                "artist": self.artist,
                "album": self.album,
                "cover_art": self.cover_art,
                "is_playing": self.is_playing,
                "updated_at": self.updated_at,
            }


class MetadataReader(threading.Thread):
    """Background thread that tails the shairport-sync metadata pipe."""

    def __init__(self, pipe_path: str, now_playing: NowPlaying):
        super().__init__(daemon=True)
        self.pipe_path = pipe_path
        self.now_playing = now_playing
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._read_loop()
            except FileNotFoundError:
                # shairport-sync hasn't created the pipe yet; wait and retry
                time.sleep(2)
            except OSError:
                time.sleep(2)

    def _read_loop(self):
        buf = ""
        with open(self.pipe_path, "r", errors="replace") as pipe:
            while not self._stop_event.is_set():
                chunk = pipe.readline()
                if not chunk:
                    time.sleep(0.05)
                    continue
                buf += chunk
                while "</item>" in buf:
                    end = buf.index("</item>") + len("</item>")
                    block = buf[:end]
                    buf = buf[end:]
                    match = _ITEM_RE.search(block)
                    if match:
                        self._handle_item(match)

    def _handle_item(self, match: "re.Match"):
        type_ascii = _hex_to_ascii(match.group("type"))
        code_ascii = _hex_to_ascii(match.group("code"))
        raw_data = match.group("data")
        data_bytes = b""
        if raw_data:
            try:
                data_bytes = base64.b64decode(raw_data.strip())
            except binascii.Error:
                data_bytes = b""

        if type_ascii == "core":  # track metadata
            text = data_bytes.decode("utf-8", errors="replace")
            if code_ascii == "minm":
                self.now_playing.update(title=text)
            elif code_ascii == "asar":
                self.now_playing.update(artist=text)
            elif code_ascii == "asal":
                self.now_playing.update(album=text)

        elif type_ascii == "ssnc":  # session/state + artwork
            if code_ascii == "PICT" and data_bytes:
                self.now_playing.update(cover_art=bytes(data_bytes))
            elif code_ascii in ("pbeg", "pres", "prsm"):
                self.now_playing.update(is_playing=True)
            elif code_ascii in ("pend", "pfls"):
                self.now_playing.update(is_playing=False)
            elif code_ascii == "pcst":
                self.now_playing.update(cover_art=None)


# ============================================================
# 3. AUDIO ANALYZER
# ============================================================


class AudioAnalyzer:
    """Captures PCM audio from the ALSA loopback device and computes
    smoothed FFT bar levels + overall volume."""

    def __init__(self):
        self._lock = threading.Lock()
        self._bars = np.zeros(NUM_BARS, dtype=np.float32)
        self._volume = 0.0
        self._stream = None
        nyquist = SAMPLE_RATE / 2
        self._band_edges = np.geomspace(40, nyquist * 0.98, NUM_BARS + 1)

    def start(self):
        self._stream = sd.InputStream(
            device=AUDIO_DEVICE,
            channels=CHANNELS,
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()

    def _callback(self, indata, frames, time_info, status):
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata

        windowed = mono * np.hanning(len(mono))
        spectrum = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(mono), d=1.0 / SAMPLE_RATE)

        new_bars = np.zeros(NUM_BARS, dtype=np.float32)
        for i in range(NUM_BARS):
            lo, hi = self._band_edges[i], self._band_edges[i + 1]
            mask = (freqs >= lo) & (freqs < hi)
            if mask.any():
                magnitude = spectrum[mask].mean()
                new_bars[i] = np.log1p(magnitude) / 6.0

        new_bars = np.clip(new_bars, 0.0, 1.0)
        volume = float(np.sqrt(np.mean(mono**2))) if len(mono) else 0.0

        with self._lock:
            rising = new_bars > self._bars
            self._bars[rising] = (
                self._bars[rising] * (1 - BAR_SMOOTHING_ATTACK)
                + new_bars[rising] * BAR_SMOOTHING_ATTACK
            )
            falling = ~rising
            self._bars[falling] = (
                self._bars[falling] * (1 - BAR_SMOOTHING_DECAY)
                + new_bars[falling] * BAR_SMOOTHING_DECAY
            )
            self._volume = 0.9 * self._volume + 0.1 * min(volume * 4, 1.0)

    def get_bars(self):
        with self._lock:
            return self._bars.copy()

    def get_volume(self):
        with self._lock:
            return self._volume


# ============================================================
# 4. VISUALIZER (rendering)
# ============================================================


def _lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


class Visualizer:
    def __init__(self):
        pygame.init()
        flags = pygame.FULLSCREEN if FULLSCREEN else 0
        self.screen = pygame.display.set_mode(
            (0, 0) if FULLSCREEN else WINDOW_SIZE, flags
        )
        pygame.mouse.set_visible(False)
        self.width, self.height = self.screen.get_size()

        self.font_title = pygame.font.SysFont(FONT_NAME, 54, bold=True)
        self.font_artist = pygame.font.SysFont(FONT_NAME, 34)
        self.font_small = pygame.font.SysFont(FONT_NAME, 24)

        self._art_cache_bytes = None
        self._art_surface = None

    def _get_album_art_surface(self, cover_art_bytes):
        if not cover_art_bytes:
            return None
        if cover_art_bytes == self._art_cache_bytes and self._art_surface:
            return self._art_surface
        try:
            image = pygame.image.load(io.BytesIO(cover_art_bytes))
            image = pygame.transform.smoothscale(image, (self.width, self.height))
        except pygame.error:
            return None
        self._art_cache_bytes = cover_art_bytes
        self._art_surface = image
        return image

    def _draw_background(self, cover_art_bytes):
        self.screen.fill(BLACK)
        if SHOW_ALBUM_ART_BACKGROUND:
            art = self._get_album_art_surface(cover_art_bytes)
            if art:
                dark = pygame.Surface((self.width, self.height))
                dark.set_alpha(int(255 * (1 - BACKGROUND_DIM)))
                dark.fill(BLACK)
                self.screen.blit(art, (0, 0))
                self.screen.blit(dark, (0, 0))

    def _draw_bars(self, bars, area_top, area_height):
        bar_area_width = self.width - (NUM_BARS + 1) * BAR_GAP_PX
        bar_width = bar_area_width / NUM_BARS
        x = BAR_GAP_PX
        for level in bars:
            bar_h = max(4, int(level * area_height))
            color = _lerp_color(BAR_COLOR_BOTTOM, BAR_COLOR_TOP, level)
            rect = pygame.Rect(
                int(x), area_top + area_height - bar_h, int(bar_width), bar_h
            )
            pygame.draw.rect(self.screen, color, rect, border_radius=4)
            x += bar_width + BAR_GAP_PX

    def _draw_text_block(self, now_playing):
        title = now_playing["title"] or "Nothing playing"
        artist = now_playing["artist"] or ""
        album = now_playing["album"] or ""

        y = 60
        title_surf = self.font_title.render(title, True, WHITE)
        self.screen.blit(title_surf, (60, y))
        y += title_surf.get_height() + 10

        if artist:
            artist_surf = self.font_artist.render(artist, True, GRAY)
            self.screen.blit(artist_surf, (60, y))
            y += artist_surf.get_height() + 4

        if album:
            album_surf = self.font_small.render(album, True, GRAY)
            self.screen.blit(album_surf, (60, y))

    def _draw_idle_screen(self):
        self.screen.fill(BLACK)
        msg1 = self.font_title.render("Waiting for AirPlay…", True, WHITE)
        msg2 = self.font_artist.render(
            f'Connect via AirPlay to "{AIRPLAY_NAME}"', True, GRAY
        )
        self.screen.blit(
            msg1, (self.width // 2 - msg1.get_width() // 2, self.height // 2 - 60)
        )
        self.screen.blit(
            msg2, (self.width // 2 - msg2.get_width() // 2, self.height // 2 + 10)
        )

    def draw_frame(self, now_playing, bars, volume):
        if not now_playing["title"] and not now_playing["is_playing"]:
            self._draw_idle_screen()
            pygame.display.flip()
            return

        self._draw_background(now_playing["cover_art"])
        self._draw_text_block(now_playing)

        bars_top = int(self.height * 0.55)
        bars_height = int(self.height * 0.35)
        self._draw_bars(bars, bars_top, bars_height)

        dot_radius = max(4, int(volume * 20))
        pygame.draw.circle(self.screen, WHITE, (self.width - 40, 40), dot_radius)

        pygame.display.flip()

    def quit(self):
        pygame.quit()


# ============================================================
# 5. MAIN
# ============================================================


def main():
    now_playing = NowPlaying()
    metadata_reader = MetadataReader(METADATA_PIPE, now_playing)
    metadata_reader.start()

    audio = AudioAnalyzer()
    try:
        audio.start()
    except Exception as e:
        print(
            f"WARNING: could not open audio device ({e}). "
            f"Visualizer will run without spectrum bars.",
            file=sys.stderr,
        )

    viz = Visualizer()
    clock = pygame.time.Clock()

    running = True
    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    running = False

            snapshot = now_playing.snapshot()
            bars = audio.get_bars()
            volume = audio.get_volume()
            viz.draw_frame(snapshot, bars, volume)
            clock.tick(FPS)
    finally:
        audio.stop()
        metadata_reader.stop()
        viz.quit()


if __name__ == "__main__":
    main()