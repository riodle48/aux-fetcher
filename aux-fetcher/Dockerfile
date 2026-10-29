ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:bookworm
FROM ${BUILD_FROM}

# yt-dlp needs Python + a JS runtime (node) for YouTube; ffmpeg for the odd format that needs remuxing.
# Chromium + Playwright drive the Spotify developer dashboard (adding friends to the app's allow-list).
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip nodejs ffmpeg ca-certificates chromium fonts-liberation \
 && pip3 install --no-cache-dir --break-system-packages "yt-dlp[default]" playwright \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV AUX_CHROMIUM=/usr/bin/chromium
COPY run.py /run.py
CMD ["python3", "-u", "/run.py"]
