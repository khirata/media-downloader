import os
import re
import json
import subprocess
import time
import boto3
from datetime import datetime, timedelta
import sys
import shlex
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

# Configurations
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
AWS_REGION = os.environ.get('AWS_REGION', 'ap-northeast-1')
DOWNLOAD_DIR = "/app/downloads"
CREATE_READY_FILE = os.environ.get('CREATE_READY_FILE', 'false').lower() == 'true'
YT_DLP_ARGS_STR = os.environ.get('YT_DLP_ARGS', '')
GLOBAL_YT_DLP_ARGS = shlex.split(YT_DLP_ARGS_STR) if YT_DLP_ARGS_STR else []
FAILURE_NOTIFICATION_URL = os.environ.get('FAILURE_NOTIFICATION_URL', '')
SUCCESS_NOTIFICATION_URL = os.environ.get('SUCCESS_NOTIFICATION_URL', '')

sqs = boto3.client('sqs', region_name=AWS_REGION)

_UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|]')
# Leave room for extensions and yt-dlp intermediate suffixes (e.g. .f251.webm.part)
_MAX_FILENAME_STEM_BYTES = 180


def sanitize_description(desc):
    """Replace characters that are unsafe in filenames."""
    return _UNSAFE_FILENAME_CHARS.sub('_', desc)


def truncate_filename(name, max_bytes=_MAX_FILENAME_STEM_BYTES):
    """Truncate a filename stem to fit within max_bytes when UTF-8 encoded."""
    encoded = name.encode('utf-8')
    if len(encoded) <= max_bytes:
        return name
    truncated = encoded[:max_bytes]
    return truncated.decode('utf-8', errors='ignore')


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _extract_first_url(msg_body):
    """Extract the first relevant URL from an SQS message body string."""
    try:
        data = json.loads(msg_body)
    except (json.JSONDecodeError, TypeError):
        return None

    # Podcast or tver/youtube: direct url field
    if data.get('url'):
        return data['url']

    # Radiko time-shift: construct from station_id + first start_time
    station_id = data.get('station_id')
    start_times = data.get('start_times') or []
    if not start_times and data.get('start_time'):
        start_times = [data['start_time']]
    if station_id and start_times:
        return f"https://radiko.jp/#!/ts/{station_id}/{start_times[0]}00"

    return None


def _fetch_radiko_title(station_id, start_time):
    """
    Look up the program title from the Radiko schedule API.
    start_time is a 12-digit string (YYYYMMDDHHmm); the API uses 14-digit ft values.
    Falls back to trying the previous day's schedule for programs starting between
    midnight and ~05:00 (Radiko's day boundary).
    Returns None on any failure or if no match is found; never raises.
    """
    try:
        ft = start_time + "00"
        base_date = datetime.strptime(start_time[:8], "%Y%m%d")
    except Exception as e:
        log(f"Failed to parse start_time for Radiko title lookup: {e}")
        return None

    for delta in (0, -1):
        date = (base_date + timedelta(days=delta)).strftime("%Y%m%d")
        api_url = f"https://radiko.jp/v3/program/station/date/{date}/{station_id}.xml"
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0 (compatible; media-downloader/1.0)"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                root = ET.fromstring(resp.read())
            for prog in root.iter('prog'):
                if prog.get('ft') == ft:
                    title_el = prog.find('title')
                    return title_el.text if title_el is not None and title_el.text else None
        except Exception as e:
            log(f"Failed to fetch Radiko program title ({date}/{station_id}): {e}")
    return None


def _extract_radiko_title_from_message(msg_body):
    """Fetch Radiko program title from the schedule API for time-shift messages."""
    try:
        data = json.loads(msg_body)
    except (json.JSONDecodeError, TypeError):
        return None

    station_id = data.get('station_id')
    start_times = data.get('start_times') or []
    if not start_times and data.get('start_time'):
        start_times = [data['start_time']]

    if station_id and start_times:
        return _fetch_radiko_title(station_id, start_times[0])
    return None


def _build_webhook_payload(url, payload_dict):
    """Build a webhook-compatible payload based on the target URL."""
    status = payload_dict.get("status", "unknown").upper()
    worker = payload_dict.get("worker", "unknown")
    title = payload_dict.get("title", "")
    source_url = payload_dict.get("url", "")
    timestamp = payload_dict.get("timestamp", "")

    lines = [f"[{status}] {worker}"]
    if title:
        lines.append(title)
    if source_url:
        lines.append(source_url)
    lines.append(timestamp)
    text = "\n".join(lines)

    if "discord.com/api/webhooks/" in url:
        return {"content": text}
    if "hooks.slack.com" in url:
        return {"text": text}
    # Generic fallback: include structured fields
    return payload_dict


def _post_notification(url, payload_dict):
    """POST a notification to the given URL (Discord/Slack webhook compatible)."""
    if not url:
        return
    payload = json.dumps(_build_webhook_payload(url, payload_dict)).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (compatible; media-downloader/1.0)"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"Notification sent (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        log(f"Failed to send notification: HTTP {e.code} — {e.read().decode(errors='replace')}")
    except Exception as e:
        log(f"Failed to send notification: {e}")


def check_truncation(file_path):
    """
    Detect truncated media files by comparing container-reported duration
    against the actual last packet timestamp.
    Returns True if the file appears complete, False if truncated.
    """
    r1 = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", file_path],
        capture_output=True, text=True)
    try:
        header_dur = float(r1.stdout.strip())
    except ValueError:
        log(f"Truncation check: unreadable duration — {file_path}")
        return False

    r2 = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "packet=pts_time",
         "-of", "csv=p=0", file_path],
        capture_output=True, text=True)
    valid_pts = []
    for line in r2.stdout.strip().split('\n'):
        try:
            valid_pts.append(float(line.strip().rstrip(',')))
        except ValueError:
            continue
    if not valid_pts:
        log(f"Truncation check: no readable packet timestamps — {file_path}")
        return False
    last_pts = valid_pts[-1]

    gap = header_dur - last_pts
    threshold = max(10.0, header_dur * 0.02)
    if gap > threshold:
        log(f"Truncation detected: last packet {last_pts:.1f}s, header {header_dur:.1f}s, gap {gap:.1f}s — {file_path}")
        return False

    log(f"Integrity OK: {header_dur:.1f}s, last packet {last_pts:.1f}s — {file_path}")
    return True


def _finalize_file(final_file_path):
    """Chowns a file and optionally creates a .ready marker."""
    puid = os.environ.get('PUID', '').strip()
    pgid = os.environ.get('PGID', '').strip()

    if puid.isdigit() and pgid.isdigit():
        try:
            os.chown(final_file_path, int(puid), int(pgid))
            log(f"Changed ownership of {final_file_path} to {puid}:{pgid}")
        except Exception as e:
            log(f"Failed to change ownership: {e}")

    if CREATE_READY_FILE:
        ready_file = f"{final_file_path}.ready"
        try:
            with open(ready_file, 'w'):
                pass
            log(f"Created ready marker file: {ready_file}")
            if puid.isdigit() and pgid.isdigit():
                os.chown(ready_file, int(puid), int(pgid))
        except Exception as e:
            log(f"Failed to create or chown ready marker file: {e}")


def run_main(worker_name, process_message_fn):
    """SQS long-poll loop. Delegates message handling to process_message_fn."""
    if not SQS_QUEUE_URL:
        log("Error: SQS_QUEUE_URL is not set.")
        sys.exit(1)

    log(f"Worker started. Listening to {SQS_QUEUE_URL}...")

    while True:
        try:
            response = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL, MaxNumberOfMessages=1,
                WaitTimeSeconds=20, VisibilityTimeout=3600
            )
            if 'Messages' in response:
                log(f"Received message: {response}")
                for message in response['Messages']:
                    receipt_handle = message['ReceiptHandle']
                    success, worker_title = process_message_fn(message['Body'])
                    source_url = _extract_first_url(message['Body'])
                    # For Radiko time-shift, fetch the actual program title from the schedule API.
                    # worker_title takes precedence (e.g. TVer title from yt-dlp).
                    try:
                        radiko_title = _extract_radiko_title_from_message(message['Body'])
                    except Exception as e:
                        log(f"Title lookup failed, continuing without title: {e}")
                        radiko_title = None
                    title = worker_title or radiko_title or ""
                    notification = {
                        "worker": worker_name,
                        "title": title,
                        "url": source_url or "",
                        "message": message['Body'],
                        "timestamp": datetime.now().isoformat(),
                    }
                    if success == "duplicate":
                        sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)
                        log("Duplicate detected. Dropping message from SQS.")
                    elif success:
                        sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)
                        log("Message processed and deleted from SQS.")
                        _post_notification(SUCCESS_NOTIFICATION_URL, {**notification, "status": "success"})
                    else:
                        _post_notification(FAILURE_NOTIFICATION_URL, {**notification, "status": "failed"})
        except Exception as e:
            log(f"SQS Polling Error: {e}")
            time.sleep(10)
