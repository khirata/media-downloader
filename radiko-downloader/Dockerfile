FROM python:3.11-slim-bookworm

# Install system dependencies (ffmpeg, cron)
RUN apt-get update && \
    apt-get install -y ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# Install yt-dlp
# Pinned version reference (currently using latest): 2026.03.03
# SHA256: bc7b7b6a8a438571117efef98a0f9a12e634f6bbcd834896cddb6a1deedc9c4d
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && \
    chmod 755 /usr/local/bin/yt-dlp

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and scripts
COPY src/ ./src/
COPY config /.config/yt-dlp/

# Start the SQS Polling loop directly (the -u flag forces real-time logging)
CMD ["python", "-u", "src/worker.py"]
