#!/bin/bash

# 1. Setup Cron environment variables
# Cron runs in a limited shell, so we dump env vars (like AWS creds) for it to use
printenv | grep -v "no_proxy" >> /etc/environment

# 2. Start Cron in the background
service cron start

# 3. Start the SQS Worker in the foreground
python3 /app/src/worker.py
