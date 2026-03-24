import os
import json
import subprocess
import tempfile
import sys

from worker_common import (
    DOWNLOAD_DIR, GLOBAL_YT_DLP_ARGS,
    log, sanitize_description, truncate_filename,
    check_truncation, _finalize_file, run_main,
)


def record_video(url, description=None):
    """
    Downloads video URL via yt-dlp.
    """
    # Generate a temporary file to store the exact paths of the downloaded files
    with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as tmpf:
        filepath_log = tmpf.name

    # Build output template: prefer description over auto-title.
    # %(title).180B limits the title to 180 UTF-8 bytes, preventing ENAMETOOLONG.
    if description:
        safe_name = truncate_filename(sanitize_description(description))
        output_tmpl = os.path.join(DOWNLOAD_DIR, f"{safe_name}.%(ext)s")
    else:
        output_tmpl = os.path.join(DOWNLOAD_DIR, "%(title).180B.%(ext)s")

    # We tell yt-dlp to append the absolute path of every generated/moved file to our tmp log
    cmd = ["yt-dlp", "-o", output_tmpl, "--print-to-file", "after_move:filepath", filepath_log, url]
    cmd.extend(GLOBAL_YT_DLP_ARGS)

    try:
        log(f"Downloading Video URL: {url}")
        subprocess.run(cmd, check=True)
        log(f"Successfully downloaded {url}")

        if os.path.exists(filepath_log):
            with open(filepath_log, 'r', encoding='utf-8') as f:
                written_files = [l for l in f.read().splitlines() if l.strip()]

            if not written_files:
                log(f"No files written for {url} — yt-dlp may have failed silently")
                os.remove(filepath_log)
                return False

            for downloaded_file in written_files:
                if downloaded_file.strip() and os.path.exists(downloaded_file):
                    if not check_truncation(downloaded_file):
                        log(f"Aborting: truncated file detected. Cleaning up.")
                        os.remove(filepath_log)
                        return False
                    _finalize_file(downloaded_file)

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
    description = data.get('description')

    if not url:
        log("Missing url in message")
        return False

    if not url.startswith(('https://', 'http://')):
        log(f"Rejected URL with invalid scheme: {url}")
        return False

    return record_video(url, description)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        url = sys.argv[1]
        log(f"Manual override: downloading {url}")
        record_video(url)
    else:
        run_main("tver-downloader", process_message)
