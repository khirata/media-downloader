FROM python:3.11-slim-bookworm

# Install system dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps. yt-dlp and yt-dlp-rajiko come from requirements.txt and
# are deliberately unpinned: upstream breaks every few weeks, so the newest
# release is the working one. The versions baked in here are only a floor -- the
# worker upgrades them on start (see YT_DLP_AUTO_UPDATE) so a container that has
# been running for months does not keep serving a stale extractor.
COPY radiko-downloader/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and scripts
COPY radiko-downloader/src/ ./src/
COPY shared/ ./src/
COPY radiko-downloader/config /.config/yt-dlp/

# Start the SQS Polling loop directly (the -u flag forces real-time logging)
CMD ["python", "-u", "src/worker.py"]
