
# Event-Driven Radiko & TVer Recording Architecture

This architecture replaces rigid, minute-by-minute `cron` polling with a stateful, event-driven scheduling system natively integrated with Debian Linux.

### How it Works

1. **The Brain (`planner.py`):** Reads the JSON schedule, calculates Japan Standard Time (JST), and dynamically queues one-off execution triggers in the Linux `at` daemon.
2. **The Muscle (`fire_api.py`):** Wakes up at the exact triggered minute, builds the payload, and fires it to the AWS SQS FIFO queue via the API.
3. **The Watcher (`systemd`):** Monitors the `shows.json` file. If a change is saved, it instantly tells the Brain to flush the old schedule and rebuild the queue.
4. **The Daily Reset (`cron`):** Runs the Brain once a day at midnight JST to schedule the upcoming day's shows.

---

## Step 1: System Prerequisites

Ensure the Linux `at` daemon is installed and enabled, and create the necessary folder structure.

```bash
# Install the 'at' daemon
sudo apt update
sudo apt install at
sudo systemctl enable --now atd

# Create required directories
mkdir -p /home/khirata/scripts
mkdir -p /home/khirata/logs
mkdir -p ~/.config/systemd/user/

```

---

## Step 2: Configuration Files

### 1. The Environment (`/home/khirata/.env`)

Stores your API credentials safely outside of your code.

```ini
MEDIA_RECORDER_API_ENDPOINT="https://your-api-id.execute-api.us-west-2.amazonaws.com/prod/record"
MEDIA_RECORDER_API_KEY="your-api-key"
MEDIA_RECORDER_SECRET="your-secret"

```

### 2. The Schedule (`/home/khirata/scripts/shows.json`)

The central configuration file. The system will automatically detect when this file is saved and update the schedule in real-time.

```json
[
  {
    "day": "Sun",
    "ready_time": "1700",
    "station": "FMJ",
    "start_times": ["1300", "1400", "1500", "1600"],
    "description": "TOKIO HOT 100"
  },
  {
    "day": "*",
    "ready_time": "2300",
    "station": "FMT",
    "start_times": ["2200"],
    "description": "Daily Late Night Jazz"
  }
]

```

---

## Step 3: The Core Python Scripts

### 1. The Muscle (`/home/khirata/scripts/fire_api.py`)

Make executable: `chmod +x /home/khirata/scripts/fire_api.py`

```python
#!/usr/bin/env python3
import sys
import os
import urllib.request
from urllib.error import URLError, HTTPError

# 1. Manually load the .env file (Zero dependencies)
env_path = '/home/khirata/.env'
if os.path.exists(env_path):
    with open(env_path, 'r') as f:
        for line in f:
            if line.strip() and not line.startswith('#'):
                key, val = line.strip().split('=', 1)
                os.environ[key] = val.strip(' "\'')

endpoint = os.environ.get('MEDIA_RECORDER_API_ENDPOINT')
api_key = os.environ.get('MEDIA_RECORDER_API_KEY')
api_secret = os.environ.get('MEDIA_RECORDER_SECRET')

# 2. Grab the JSON payload passed from the 'at' daemon
try:
    payload_str = sys.argv[1]
    payload_bytes = payload_str.encode('utf-8')
except IndexError:
    print("Error: No JSON payload provided.")
    sys.exit(1)

# 3. Fire the API Request
req = urllib.request.Request(endpoint, data=payload_bytes, method='POST', headers={
    'Content-Type': 'application/json',
    'x-api-key': api_key,
    'x-api-secret': api_secret
})

try:
    with urllib.request.urlopen(req) as response:
        print(f"Success! HTTP Status: {response.status}")
except HTTPError as e:
    print(f"API Error: HTTP {e.code} - {e.reason}")
except URLError as e:
    print(f"Network Error: Failed to reach API - {e.reason}")

```

### 2. The Brain (`/home/khirata/scripts/planner.py`)

Make executable: `chmod +x /home/khirata/scripts/planner.py`

```python
#!/usr/bin/env python3
import json
import subprocess
import os
import sys
import shlex
from datetime import datetime
from zoneinfo import ZoneInfo

SCHEDULE_FILE = "/home/khirata/scripts/shows.json"
MUSCLE_SCRIPT = "/home/khirata/scripts/fire_api.py"

# Enforce Japan Standard Time context
jst = ZoneInfo("Asia/Tokyo")
now_jst = datetime.now(jst)

current_day = now_jst.strftime("%a")      # e.g., "Sun"
target_date = now_jst.strftime("%Y%m%d")  # e.g., "20260314"

print(f"Planning Radiko downloads for {current_day}, {target_date}...")

# 1. WIPE THE SLATE CLEAN (Idempotency)
try:
    atq_output = subprocess.check_output(['atq'], text=True)
    for line in atq_output.strip().split('\n'):
        if line:
            job_id = line.split()[0]
            subprocess.run(['atrm', job_id])
    print("Cleared existing schedule. Rebuilding from shows.json...")
except FileNotFoundError:
    print("Error: The Linux 'at' daemon is not installed. Run: sudo apt install at")
    sys.exit(1)

# 2. READ JSON & BUILD QUEUE
try:
    with open(SCHEDULE_FILE, 'r') as f:
        shows = json.load(f)
except Exception as e:
    print(f"Failed to read {SCHEDULE_FILE}: {e}")
    sys.exit(1)

for show in shows:
    day = show.get("day")
    
    # Check if the show airs today
    if day == current_day or day == "*":
        ready_time = show.get("ready_time")
        station = show.get("station")
        desc = show.get("description")
        start_times = show.get("start_times", [])

        # Build Radiko URLs
        urls = [f"https://radiko.jp/#!/ts/{station}/{target_date}{t}00" for t in start_times]

        # Build AWS Payload
        payload_str = json.dumps({
            "description": desc,
            "urls": urls
        })

        safe_payload = shlex.quote(payload_str)
        fmt_time = f"{ready_time[:2]}:{ready_time[2:]}"
        
        cmd = f"{MUSCLE_SCRIPT} {safe_payload} >> /home/khirata/logs/radiko-api.log 2>&1"

        # Pass to the 'at' daemon, enforcing JST timezone
        env = os.environ.copy()
        env['TZ'] = 'Asia/Tokyo'
        
        process = subprocess.Popen(
            ['at', fmt_time], 
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
            env=env, text=True
        )
        process.communicate(input=cmd)

        print(f"Queued: {desc} -> Will trigger at exactly {fmt_time} JST")

```

---

## Step 4: Systemd Event Watcher

These files tell Linux to watch `shows.json` and automatically run the planner if you make edits.

**1. Create Service Unit (`~/.config/systemd/user/radiko-planner.service`)**

```ini
[Unit]
Description=Run Radiko Daily Planner

[Service]
Type=oneshot
ExecStart=/home/khirata/scripts/planner.py

```

**2. Create Path Unit (`~/.config/systemd/user/radiko-planner.path`)**

```ini
[Unit]
Description=Watch shows.json for modifications

[Path]
PathModified=/home/khirata/scripts/shows.json
Unit=radiko-planner.service

[Install]
WantedBy=default.target

```

**3. Enable the Watcher**

```bash
systemctl --user daemon-reload
systemctl --user enable --now radiko-planner.path
sudo loginctl enable-linger khirata

```

---

## Step 5: Daily Crontab Reset

Ensure the schedule is rebuilt cleanly at the start of every day (Japan Time).

Run `crontab -e` and add:

```text
CRON_TZ=Asia/Tokyo
# Wake up at 12:05 AM JST every day to plan out the day's recordings
5 0 * * * /home/khirata/scripts/planner.py >> /home/khirata/logs/planner.log 2>&1

```

---

## Managing the System (Cheat Sheet)

* **View currently queued jobs:** `atq`
* **Delete a queued job:** `atrm <job_number>`
* **Force a manual queue rebuild:** `/home/khirata/scripts/planner.py`
* **View API execution logs:** `tail -f /home/khirata/logs/radiko-api.log`
* **View daily planner logs:** `tail -f /home/khirata/logs/planner.log`

---

Would you like me to walk you through running a test of the `radiko-planner.path` watcher to confirm that `systemd` successfully detects file saves on your server?