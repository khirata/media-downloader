#!/bin/bash

# 1. Setup Cron environment variables
# Cron runs in a limited shell; export only the specific vars it needs.
# Never use `printenv` here — it would write sensitive credentials to
# world-readable /etc/environment, exposing them to any process in the container.
for var in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION SQS_QUEUE_URL \
           GDRIVE_FOLDER_ID PUID PGID TZ YT_DLP_ARGS CREATE_READY_FILE FAILURE_NOTIFICATION_URL; do
    if [ -n "${!var}" ]; then
        printf '%s=%s\n' "$var" "${!var}" >> /etc/environment
    fi
done

# 2. Start Cron in the background
service cron start

# 3. Start the SQS Worker in the foreground
python3 /app/src/worker.py
