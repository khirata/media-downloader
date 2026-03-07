import os
import json
import subprocess
import time
import boto3
from datetime import datetime
import sys
import shlex
import tempfile

# Configurations
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
AWS_REGION = os.environ.get('AWS_REGION', 'ap-northeast-1')
DOWNLOAD_DIR = "/app/downloads"
YT_DLP_ARGS_STR = os.environ.get('YT_DLP_ARGS', '')
GLOBAL_YT_DLP_ARGS = shlex.split(YT_DLP_ARGS_STR) if YT_DLP_ARGS_STR else []

sqs = boto3.client('sqs', region_name=AWS_REGION)

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

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
                    if puid.isdigit() and pgid.isdigit():
                        try:
                            os.chown(downloaded_file, int(puid), int(pgid))
                            log(f"Changed ownership of {downloaded_file} to {puid}:{pgid}")
                        except Exception as e:
                            log(f"Failed to change ownership of {downloaded_file}: {e}")
            
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
            log(f"Messages: {response}")
            if 'Messages' in response:
                for message in response['Messages']:
                    receipt_handle = message['ReceiptHandle']
                    success = process_message(message['Body'])
                    if success:
                        sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)
                        log("Message processed and deleted from SQS.")
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
