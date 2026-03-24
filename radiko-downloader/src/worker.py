import os
import re
import json
import subprocess
import tempfile
import time
import boto3
from datetime import datetime
import sys
import glob
import shlex
import urllib.request

# Google API Imports
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Configurations
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
AWS_REGION = os.environ.get('AWS_REGION', 'ap-northeast-1')
DOWNLOAD_DIR = "/app/downloads"
GDRIVE_FOLDER_ID = os.environ.get('GDRIVE_FOLDER_ID')
CREATE_READY_FILE = os.environ.get('CREATE_READY_FILE', 'false').lower() == 'true'
YT_DLP_ARGS_STR = os.environ.get('YT_DLP_ARGS', '')
GLOBAL_YT_DLP_ARGS = shlex.split(YT_DLP_ARGS_STR) if YT_DLP_ARGS_STR else []
FAILURE_NOTIFICATION_URL = os.environ.get('FAILURE_NOTIFICATION_URL', '')
SUCCESS_NOTIFICATION_URL = os.environ.get('SUCCESS_NOTIFICATION_URL', '')

sqs = boto3.client('sqs', region_name=AWS_REGION)

_UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|]')
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
        "worker": "radiko-downloader",
        "message": msg_body,
        "timestamp": datetime.now().isoformat(),
    })

def notify_success(msg_body):
    """POST a success notification to SUCCESS_NOTIFICATION_URL if configured."""
    _post_notification(SUCCESS_NOTIFICATION_URL, {
        "status": "success",
        "worker": "radiko-downloader",
        "message": msg_body,
        "timestamp": datetime.now().isoformat(),
    })

def check_truncation(file_path):
    """
    Detect truncated audio files by comparing container-reported duration
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

def upload_to_gdrive(local_file_path, file_name):
    token_path = '/app/token.json'
    
    if not GDRIVE_FOLDER_ID or not os.path.exists(token_path):
        log("Google Drive token.json or Folder ID missing. Skipping upload.")
        return "SKIPPED"

    creds = Credentials.from_authorized_user_file(token_path, ['https://www.googleapis.com/auth/drive.file'])

    if creds.expired and creds.refresh_token:
        log("Refreshing Google Drive token...")
        try:
            creds.refresh(Request())
            with open(token_path, 'w') as f:
                f.write(creds.to_json())
        except Exception as e:
            log(f"Failed to refresh token: {e}")
            return False

    log(f"Uploading {file_name} to Google Drive...")
    try:
        service = build('drive', 'v3', credentials=creds)
        file_metadata = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
        media = MediaFileUpload(local_file_path, resumable=True)
        uploaded_file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        
        log(f"Successfully uploaded. File ID: {uploaded_file.get('id')}")
        return True
    except Exception as e:
        log(f"Google Drive Upload Error: {e}")
        return False

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

def record_radiko(station_id, start_times, description=None):
    """
    Downloads Radiko segments based solely on start_times.
    yt-dlp automatically handles downloading until the program's natural end.
    """
    downloaded_files = []

    # 1. Download all segments
    for i, start_time in enumerate(start_times):
        url = f"https://radiko.jp/#!/ts/{station_id}/{start_time}00"
        file_prefix = f"part{i}-{start_time}-{station_id}"
        output_path_template = os.path.join(DOWNLOAD_DIR, f"{file_prefix}.%(ext)s")

        # Base command, omitting --ignore-config to allow yt-dlp.conf overrides
        cmd = ["yt-dlp", "--no-part", "-o", output_path_template, url]

        # Append global (env) args only — never args from SQS message bodies
        cmd.extend(GLOBAL_YT_DLP_ARGS)

        try:
            log(f"Downloading segment {i+1}/{len(start_times)}: {start_time}")
            subprocess.run(cmd, check=True)

            search_pattern = os.path.join(DOWNLOAD_DIR, f"{file_prefix}.*")
            files = glob.glob(search_pattern)
            if files:
                downloaded_files.append(files[0])
            else:
                log(f"Could not find output file for {start_time}")
                return False
        except subprocess.CalledProcessError as e:
            log(f"Error downloading {start_time}: {e}")
            return False

    if not downloaded_files:
        return False

    # 2. Determine final clean file name
    first_start = start_times[0]
    ext = downloaded_files[0].split('.')[-1]

    if description:
        safe_desc = truncate_filename(sanitize_description(description))
        final_file_name = f"{first_start}-{station_id}-{safe_desc}.{ext}"
    else:
        final_file_name = f"{first_start}-{station_id}.{ext}"

    final_file_path = os.path.join(DOWNLOAD_DIR, final_file_name)

    # 3. Concatenate (or just rename if only 1 segment)
    if len(downloaded_files) > 1:
        log(f"Concatenating audio segments into {final_file_name}...")
        concat_list_path = os.path.join(DOWNLOAD_DIR, "concat_inputs.txt")
        with open(concat_list_path, 'w') as f:
            for df in downloaded_files:
                f.write(f"file '{df}'\n")

        concat_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path, "-c", "copy", final_file_path]
        subprocess.run(concat_cmd, check=True)
        os.remove(concat_list_path)
    else:
        os.rename(downloaded_files[0], final_file_path)

    # 4. Verify integrity before upload
    if not check_truncation(final_file_path):
        log("Aborting: truncated file detected. Cleaning up.")
        for df in downloaded_files:
            if os.path.exists(df):
                os.remove(df)
        if os.path.exists(final_file_path):
            os.remove(final_file_path)
        return False

    # 5. Upload & Cleanup
    upload_status = upload_to_gdrive(final_file_path, final_file_name)

    if upload_status is True:
        log("Cleaning up all local files...")
        for df in downloaded_files:
            if os.path.exists(df):
                os.remove(df)
        if os.path.exists(final_file_path):
            os.remove(final_file_path)
        return True
    elif upload_status == "SKIPPED":
        log(f"Upload skipped. Keeping final file locally at {final_file_path}.")
        _finalize_file(final_file_path)
        log("Cleaning up intermediate files...")
        for df in downloaded_files:
            if os.path.exists(df) and df != final_file_path:
                os.remove(df)
        return True

    # Upload failed — clean up all downloaded files to avoid disk accumulation
    log("Upload failed. Cleaning up downloaded files...")
    for df in downloaded_files:
        if os.path.exists(df):
            os.remove(df)
    if os.path.exists(final_file_path):
        os.remove(final_file_path)
    return False

def download_podcast(url, description=None):
    """Downloads a Radiko podcast episode directly via yt-dlp."""
    # Use a temp prefix so we can find the output file afterwards
    episode_id = url.rstrip('/').split('/')[-1]
    file_prefix = f"podcast-{episode_id}"
    output_path_template = os.path.join(DOWNLOAD_DIR, f"{file_prefix}.%(ext)s")

    cmd = ["yt-dlp", "--no-part", "-o", output_path_template, url]
    cmd.extend(GLOBAL_YT_DLP_ARGS)

    try:
        log(f"Downloading podcast episode: {url}")
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        log(f"Error downloading podcast {url}: {e}")
        return False

    files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{file_prefix}.*"))
    if not files:
        log(f"Could not find output file for podcast {episode_id}")
        return False

    downloaded_file = files[0]
    ext = downloaded_file.split('.')[-1]

    if description:
        safe_desc = truncate_filename(sanitize_description(description))
        final_file_name = f"{episode_id}-{safe_desc}.{ext}"
    else:
        final_file_name = f"{episode_id}.{ext}"

    final_file_path = os.path.join(DOWNLOAD_DIR, final_file_name)
    os.rename(downloaded_file, final_file_path)

    if not check_truncation(final_file_path):
        log("Aborting: truncated file detected. Cleaning up.")
        if os.path.exists(final_file_path):
            os.remove(final_file_path)
        return False

    upload_status = upload_to_gdrive(final_file_path, final_file_name)

    if upload_status is True:
        log("Cleaning up local file after upload...")
        if os.path.exists(final_file_path):
            os.remove(final_file_path)
        return True
    elif upload_status == "SKIPPED":
        log(f"Upload skipped. Keeping file locally at {final_file_path}.")
        _finalize_file(final_file_path)
        return True

    # Upload failed — clean up to avoid disk accumulation
    log("Upload failed. Cleaning up downloaded file...")
    if os.path.exists(final_file_path):
        os.remove(final_file_path)
    return False


def process_message(msg_body):
    """Parses SQS JSON. Expects raw delivery."""
    try:
        data = json.loads(msg_body)
    except json.JSONDecodeError:
        log("Invalid JSON received")
        return False

    description = data.get('description')

    # Podcast: {"type": "radiko", "url": "https://radiko.jp/podcast/episodes/..."}
    if data.get('url'):
        return download_podcast(data['url'], description)

    station_id = data.get('station_id')
    start_times = data.get('start_times', [])

    # Fallback for older single-segment messages
    if not start_times and data.get('start_time'):
        start_times = [data.get('start_time')]

    if not station_id or not start_times:
        log("Missing station_id or start_times in message")
        return False

    return record_radiko(station_id, start_times, description)

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
    # If run via CLI, sys.argv[1] is station_id, and everything after is a start_time
    if len(sys.argv) > 2:
        station_id = sys.argv[1]
        start_times = sys.argv[2:]
        log(f"Manual override: {station_id} combining segments: {start_times}")
        record_radiko(station_id, start_times)
    else:
        main()
