import os
import subprocess
import json
import glob
import sys
import tempfile

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from worker_common import (
    DOWNLOAD_DIR, GLOBAL_YT_DLP_ARGS, RADIRU_FIELDS_TEMPLATE,
    log, sanitize_description, truncate_filename,
    check_truncation, _finalize_file, run_main, run_download,
    _fetch_radiko_title, ensure_yt_dlp_current,
    classify_radio_url, parse_radiru_fields, radiru_filename, resolve_radiru_url,
)

GDRIVE_FOLDER_ID = os.environ.get('GDRIVE_FOLDER_ID')


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

        log(f"Downloading segment {i+1}/{len(start_times)}: {start_time}")
        if not run_download(cmd, file_prefix, start_time):
            return False

        search_pattern = os.path.join(DOWNLOAD_DIR, f"{file_prefix}.*")
        files = glob.glob(search_pattern)
        if files:
            downloaded_files.append(files[0])
        else:
            log(f"No output file for {start_time} — already downloaded or yt-dlp skipped")
            return "duplicate"

    if not downloaded_files:
        return False

    # 2. Determine final clean file name
    first_start = start_times[0]
    ext = downloaded_files[0].split('.')[-1]

    if description:
        safe_desc = truncate_filename(sanitize_description(description))
        final_file_name = f"{first_start}-{station_id}-{safe_desc}.{ext}"
    else:
        title = _fetch_radiko_title(station_id, first_start)
        if title:
            safe_title = truncate_filename(sanitize_description(title))
            final_file_name = f"{first_start}-{station_id}-{safe_title}.{ext}"
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


def _deliver_file(final_file_path, final_file_name):
    """
    Verify, upload, and clean up one finished recording.

    Returns True when the file is safely delivered — uploaded to Drive, or kept
    locally when Drive is not configured — and False when it should be treated
    as a failed download. Either way nothing is left behind on disk.
    """
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


def download_podcast(url, description=None):
    """Downloads a Radiko podcast episode directly via yt-dlp."""
    # Use a temp prefix so we can find the output file afterwards
    episode_id = url.rstrip('/').split('/')[-1]
    file_prefix = f"podcast-{episode_id}"
    output_path_template = os.path.join(DOWNLOAD_DIR, f"{file_prefix}.%(ext)s")

    cmd = ["yt-dlp", "--no-part", "-o", output_path_template, url]
    cmd.extend(GLOBAL_YT_DLP_ARGS)

    log(f"Downloading podcast episode: {url}")
    if not run_download(cmd, file_prefix, f"podcast {url}"):
        return False

    files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{file_prefix}.*"))
    if not files:
        log(f"No output file for podcast {episode_id} — already downloaded or yt-dlp skipped")
        return "duplicate"

    downloaded_file = files[0]
    ext = downloaded_file.split('.')[-1]

    if description:
        safe_desc = truncate_filename(sanitize_description(description))
        final_file_name = f"{episode_id}-{safe_desc}.{ext}"
    else:
        final_file_name = f"{episode_id}.{ext}"

    final_file_path = os.path.join(DOWNLOAD_DIR, final_file_name)
    os.rename(downloaded_file, final_file_path)

    return _deliver_file(final_file_path, final_file_name)


def download_radiru(url, description=None):
    """
    Downloads a らじる★らじる (NHK Radio) programme via yt-dlp.

    Unlike the podcast path this can produce several files: a programme URL
    expands into every episode currently in 聞き逃し. Each one is named, verified
    and delivered on its own.

    Returns (status, title) where status is True, False, or "duplicate".
    """
    resolved = resolve_radiru_url(url)
    if not resolved:
        log(f"Could not resolve らじる URL: {url}")
        return False, None
    if resolved != url:
        log(f"Resolved {url} -> {resolved}")

    # Episode ids all begin with the programme id, so this one prefix covers
    # every file the download produces — which is what purge_partial_downloads
    # needs in order to clear wreckage between retry attempts.
    programme_id = sanitize_description(resolved.rpartition('p=')[2]) or 'unknown'
    file_prefix = f"radiru-{programme_id}"

    with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as tmpf:
        fields_log = tmpf.name

    output_path_template = os.path.join(DOWNLOAD_DIR, "radiru-%(id)s.%(ext)s")

    # Omits --ignore-config so yt-dlp.conf still applies, matching record_radiko
    cmd = ["yt-dlp", "--no-part", "-o", output_path_template,
           "--print-to-file", RADIRU_FIELDS_TEMPLATE, fields_log,
           resolved]

    # Append global (env) args only — never args from SQS message bodies
    cmd.extend(GLOBAL_YT_DLP_ARGS)

    log(f"Downloading らじる programme: {resolved}")
    download_ok = run_download(cmd, file_prefix, f"らじる {resolved}")

    try:
        with open(fields_log, 'r', encoding='utf-8') as f:
            entries = parse_radiru_fields(f.read())
    except OSError as e:
        log(f"Could not read yt-dlp field output: {e}")
        entries = []
    finally:
        if os.path.exists(fields_log):
            os.remove(fields_log)

    if not download_ok:
        return False, None

    if not entries:
        log(f"No output files for {resolved} — already downloaded or yt-dlp skipped")
        return "duplicate", None

    first_title = None
    all_delivered = True

    for path, start_jst, channel, title in entries:
        if not os.path.exists(path):
            log(f"yt-dlp reported {path} but it is no longer on disk; skipping")
            all_delivered = False
            continue

        if first_title is None:
            first_title = title

        ext = path.rsplit('.', 1)[-1]
        final_file_name = radiru_filename(start_jst, channel, title, ext, description)
        final_file_path = os.path.join(DOWNLOAD_DIR, final_file_name)

        if path != final_file_path:
            os.rename(path, final_file_path)

        if not _deliver_file(final_file_path, final_file_name):
            all_delivered = False

    return all_delivered, first_title


def process_message(msg_body):
    """Parses SQS JSON. Expects raw delivery."""
    try:
        data = json.loads(msg_body)
    except json.JSONDecodeError:
        log("Invalid JSON received")
        return False, None

    description = data.get('description')

    # URL-addressed sources: Radiko podcasts and らじる★らじる (NHK Radio).
    # Time-shift recordings arrive as station_id + start_times instead.
    url = data.get('url')
    if url:
        if not isinstance(url, str) or not url.startswith(('https://', 'http://')):
            log(f"Rejected URL with invalid scheme: {url}")
            return False, None

        kind = classify_radio_url(url)
        if kind == 'radiru':
            return download_radiru(url, description)
        if kind == 'radiko_podcast':
            return download_podcast(url, description), None

        log(f"No handler for URL: {url}")
        return False, None

    station_id = data.get('station_id')
    start_times = data.get('start_times', [])

    # Fallback for older single-segment messages
    if not start_times and data.get('start_time'):
        start_times = [data.get('start_time')]

    if not station_id or not start_times:
        log("Missing station_id or start_times in message")
        return False, None

    return record_radiko(station_id, start_times, description), None


if __name__ == "__main__":
    # A single argument is a URL (らじる or a Radiko podcast); otherwise
    # sys.argv[1] is a station_id and everything after it is a start_time.
    if len(sys.argv) == 2 and classify_radio_url(sys.argv[1]):
        url = sys.argv[1]
        ensure_yt_dlp_current()
        log(f"Manual override: downloading {url}")
        if classify_radio_url(url) == 'radiru':
            download_radiru(url)
        else:
            download_podcast(url)
    elif len(sys.argv) > 2:
        station_id = sys.argv[1]
        start_times = sys.argv[2:]
        ensure_yt_dlp_current()
        log(f"Manual override: {station_id} combining segments: {start_times}")
        record_radiko(station_id, start_times)
    else:
        run_main("radiko-downloader", process_message)
