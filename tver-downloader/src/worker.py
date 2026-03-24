import os
import json
import subprocess
import time
import boto3
from datetime import datetime
import sys
import shlex
import tempfile
import urllib.request

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

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def _post_notification(url, payload_dict):
    """POST a notification to the given URL."""
    if not url:
        return
    payload = json.dumps(payload_dict).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"Notification sent (HTTP {resp.status})")
    except Exception as e:
        log(f"Failed to send notification: {e}")

def notify_failure(msg_body):
    """POST a failure notification to FAILURE_NOTIFICATION_URL if configured."""
    _post_notification(FAILURE_NOTIFICATION_URL, {
        "status": "failed",
        "worker": "tver-downloader",
        "message": msg_body,
        "timestamp": datetime.now().isoformat(),
    })

def notify_success(msg_body):
    """POST a success notification to SUCCESS_NOTIFICATION_URL if configured."""
    _post_notification(SUCCESS_NOTIFICATION_URL, {
        "status": "success",
        "worker": "tver-downloader",
        "message": msg_body,
        "timestamp": datetime.now().isoformat(),
    })

def check_truncation(file_path):
    """
    Detect truncated video files by comparing container-reported duration
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
    pts_lines = [l for l in r2.stdout.strip().split('\n') if l.strip()]
    if not pts_lines:
        log(f"Truncation check: no packets found — {file_path}")
        return False
    try:
        last_pts = float(pts_lines[-1])
    except ValueError:
        log(f"Truncation check: unreadable packet timestamp — {file_path}")
        return False

    gap = header_dur - last_pts
    threshold = max(10.0, header_dur * 0.02)
    if gap > threshold:
        log(f"Truncation detected: last packet {last_pts:.1f}s, header {header_dur:.1f}s, gap {gap:.1f}s — {file_path}")
        return False

    log(f"Integrity OK: {header_dur:.1f}s, last packet {last_pts:.1f}s — {file_path}")
    return True

def record_video(url):
    """
    Downloads video URL via yt-dlp.
    """
    # Generate a temporary file to store the exact paths of the downloaded files
    with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as tmpf:
        filepath_log = tmpf.name

    # We tell yt-dlp to append the absolute path of every generated/moved file to our tmp log
    cmd = ["yt-dlp", "-o", os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"), "--print-to-file", "after_move:filepath", filepath_log, url]
    cmd.extend(GLOBAL_YT_DLP_ARGS)

    try:
        log(f"Downloading Video URL: {url}")
        subprocess.run(cmd, check=True)
        log(f"Successfully downloaded {url}")

        # Post-download ownership adjustment
        puid = os.environ.get('PUID', '').strip()
        pgid = os.environ.get('PGID', '').strip()
        
        if os.path.exists(filepath_log):
            with open(filepath_log, 'r', encoding='utf-8') as f:
                written_files = f.read().splitlines()
            
            # Change ownership for all generated files (video, audio, subs, etc.)
            for downloaded_file in written_files:
                if downloaded_file.strip() and os.path.exists(downloaded_file):
                    
                    # 1. Chown the main file first
                    if puid.isdigit() and pgid.isdigit():
                        try:
                            os.chown(downloaded_file, int(puid), int(pgid))
                            log(f"Changed ownership of {downloaded_file} to {puid}:{pgid}")
                        except Exception as e:
                            log(f"Failed to change ownership of {downloaded_file}: {e}")
                    
                    # 2. Verify integrity
                    if not check_truncation(downloaded_file):
                        log(f"Aborting: truncated file detected. Cleaning up.")
                        os.remove(filepath_log)
                        return False

                    # 3. THEN create the ready marker
                    if CREATE_READY_FILE:
                        ready_file = f"{downloaded_file}.ready"
                        try:
                            with open(ready_file, 'w'):
                                pass
                            log(f"Created ready marker file: {ready_file}")
                            # Chown the ready marker itself to match
                            if puid.isdigit() and pgid.isdigit():
                                os.chown(ready_file, int(puid), int(pgid))
                        except Exception as e:
                            log(f"Failed to create or chown ready marker file: {e}")
            
            # Clean up out temp log
            os.remove(filepath_log)

        return True
    except subprocess.CalledProcessError as e:
        log(f"Error downloading {url}: {e}")
        
        if os.path.exists(filepath_log):
            os.remove(filepath_log)
        return False

def process_message(msg_body):
    """Parses SQS JSON. Expects raw delivery: {"url": "..."}"""
    try:
        data = json.loads(msg_body)
    except json.JSONDecodeError:
        log("Invalid JSON received")
        return False

    url = data.get('url')

    if not url:
        log("Missing url in message")
        return False

    if not url.startswith(('https://', 'http://')):
        log(f"Rejected URL with invalid scheme: {url}")
        return False

    return record_video(url)

def main():
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
                    success = process_message(message['Body'])
                    if success:
                        sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)
                        log("Message processed and deleted from SQS.")
                        notify_success(message['Body'])
                    else:
                        notify_failure(message['Body'])
        except Exception as e:
            log(f"SQS Polling Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        url = sys.argv[1]
        log(f"Manual override: downloading {url}")
        record_video(url)
    else:
        main()
