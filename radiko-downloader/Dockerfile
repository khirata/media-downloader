FROM python:3.11-slim-bookworm

# Install system dependencies (ffmpeg, cron)
RUN apt-get update && \
    apt-get install -y ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# Install yt-dlp
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && \
    chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and scripts
COPY src/ ./src/
COPY config /.config/yt-dlp/

# Start the SQS Polling loop directly (the -u flag forces real-time logging)
CMD ["python", "-u", "src/worker.py"]
