FROM python:3.11-slim-bookworm

RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps. yt-dlp comes from requirements.txt and is deliberately
# unpinned: YouTube breaks extraction every few weeks, so the newest release is
# the working one. The version baked in here is only a floor -- the worker
# upgrades yt-dlp on start (see YT_DLP_AUTO_UPDATE) so a container that has been
# running for months does not keep serving a stale extractor.
COPY tver-downloader/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY tver-downloader/src/ ./src/
COPY shared/ ./src/

# Start the SQS Polling loop directly
CMD ["python", "-u", "src/worker.py"]
