ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:3.21
FROM ${BUILD_FROM}

# yt-dlp needs Python + a JS runtime (node) for YouTube; ffmpeg for the odd format that needs remuxing.
RUN apk add --no-cache python3 py3-pip nodejs ffmpeg ca-certificates \
 && pip3 install --no-cache-dir --break-system-packages "yt-dlp[default]"

COPY run.py /run.py
CMD ["python3", "-u", "/run.py"]
