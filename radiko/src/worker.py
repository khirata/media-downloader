import os
import json
import subprocess
import time
import boto3
from datetime import datetime
import sys
import glob
import shlex

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
YT_DLP_ARGS_STR = os.environ.get('YT_DLP_ARGS', '')
GLOBAL_YT_DLP_ARGS = shlex.split(YT_DLP_ARGS_STR) if YT_DLP_ARGS_STR else []

sqs = boto3.client('sqs', region_name=AWS_REGION)

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

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
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
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

def record_radiko(station_id, start_times, yt_dlp_args_ovr=None):
    """
    Downloads Radiko segments based solely on start_times.
    yt-dlp automatically handles downloading until the program's natural end.
    """
    downloaded_files = []
    
    yt_dlp_args_ovr = yt_dlp_args_ovr or []
    
    # 1. Download all segments
    for i, start_time in enumerate(start_times):
        url = f"https://radiko.jp/#!/ts/{station_id}/{start_time}00"
        file_prefix = f"part{i}-{start_time}-{station_id}"
        output_path_template = os.path.join(DOWNLOAD_DIR, f"{file_prefix}.%(ext)s")
        
        # Base command, omitting --ignore-config to allow yt-dlp.conf overrides
        cmd = ["yt-dlp", "--no-part", "-o", output_path_template, url]
        
        # Append global (env) args, then message-specific args
        cmd.extend(GLOBAL_YT_DLP_ARGS)
        cmd.extend(yt_dlp_args_ovr)

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

    # 4. Upload & Cleanup
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
        log("Cleaning up intermediate files...")
        for df in downloaded_files:
            if os.path.exists(df) and df != final_file_path:
                os.remove(df)
        return True

    return False

def process_message(msg_body):
    """Parses SQS JSON. Expects raw delivery: {"station_id": "FMJ", "start_times": ["..."]}"""
    try:
        # Just load it directly. No envelope to unwrap!
        data = json.loads(msg_body)
    except json.JSONDecodeError:
        log("Invalid JSON received")
        return False

    station_id = data.get('station_id')
    start_times = data.get('start_times', [])
    yt_dlp_args_ovr = data.get('yt_dlp_args', [])
    
    # Fallback for older single-segment messages
    if not start_times and data.get('start_time'):
        start_times = [data.get('start_time')]

    if not station_id or not start_times:
        log("Missing station_id or start_times in message")
        return False

    return record_radiko(station_id, start_times, yt_dlp_args_ovr)

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
    # If run via CLI, sys.argv[1] is station_id, and everything after is a start_time
    if len(sys.argv) > 2:
        station_id = sys.argv[1]
        start_times = sys.argv[2:]
        log(f"Manual override: {station_id} combining segments: {start_times}")
        record_radiko(station_id, start_times)
    else:
        main()
