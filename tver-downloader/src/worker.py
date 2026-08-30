import os
import json
import subprocess
import tempfile
import sys

from worker_common import (
    DOWNLOAD_DIR, build_yt_dlp_args, parse_force,
    log, sanitize_description, truncate_filename,
    check_truncation, _finalize_file, run_main,
    ensure_yt_dlp_current, yt_dlp_version,
)


def record_video(url, description=None, force=False):
    """
    Downloads video URL via yt-dlp.

    yt-dlp skips a URL whose output file already exists, which is what keeps a
    re-published episode from being fetched twice. force=True overrides that and
    re-downloads unconditionally; note yt-dlp deletes the existing file before
    it starts, so a forced attempt that then fails leaves neither file behind.

    Returns (success: bool, title: str | None).
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as tmpf:
        filepath_log = tmpf.name
    with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as tmpf:
        title_log = tmpf.name

    # Build output template: prefer description over auto-title.
    # %(title).180B limits the title to 180 UTF-8 bytes, preventing ENAMETOOLONG.
    if description:
        safe_name = truncate_filename(sanitize_description(description))
        output_tmpl = os.path.join(DOWNLOAD_DIR, f"{safe_name}.%(ext)s")
    else:
        output_tmpl = os.path.join(DOWNLOAD_DIR, "%(title).180B.%(ext)s")

    # Capture the filepath and the original (unsanitized) title from yt-dlp.
    cmd = ["yt-dlp", "-o", output_tmpl,
           "--print-to-file", "after_move:filepath", filepath_log,
           "--print-to-file", "after_move:%(title)s", title_log,
           url]
    cmd.extend(build_yt_dlp_args(force))

    try:
        log(f"Downloading Video URL: {url}{' (forced re-download)' if force else ''}")
        subprocess.run(cmd, check=True)
        log(f"Successfully downloaded {url}")

        title = None
        if os.path.exists(title_log):
            with open(title_log, 'r', encoding='utf-8') as f:
                lines = [l for l in f.read().splitlines() if l.strip()]
                title = lines[0] if lines else None
            os.remove(title_log)

        if os.path.exists(filepath_log):
            with open(filepath_log, 'r', encoding='utf-8') as f:
                written_files = [l for l in f.read().splitlines() if l.strip()]

            if not written_files:
                log(f"No files written for {url} — already downloaded or yt-dlp skipped")
                os.remove(filepath_log)
                return "duplicate", None

            for downloaded_file in written_files:
                if downloaded_file.strip() and os.path.exists(downloaded_file):
                    if not check_truncation(downloaded_file):
                        log(f"Aborting: truncated file detected. Cleaning up.")
                        os.remove(filepath_log)
                        return False, None
                    _finalize_file(downloaded_file)

            os.remove(filepath_log)

        return True, title
    except subprocess.CalledProcessError as e:
        # Extractor breakage is the usual cause here and is invisible in the
        # exit code alone, so record which yt-dlp produced it.
        log(f"Error downloading {url} (yt-dlp {yt_dlp_version() or 'unknown'}): {e}")
        for f in (filepath_log, title_log):
            if os.path.exists(f):
                os.remove(f)
        return False, None


def process_message(msg_body):
    """Parses SQS JSON. Expects raw delivery: {"url": "..."}"""
    try:
        data = json.loads(msg_body)
    except json.JSONDecodeError:
        log("Invalid JSON received")
        return False

    url = data.get('url')
    description = data.get('description')
    force = parse_force(data)

    if not url:
        log("Missing url in message")
        return False

    if not url.startswith(('https://', 'http://')):
        log(f"Rejected URL with invalid scheme: {url}")
        return False

    return record_video(url, description, force)


if __name__ == "__main__":
    argv = sys.argv[1:]
    urls = [a for a in argv if a != "--force"]
    if urls:
        force = "--force" in argv
        url = urls[0]
        ensure_yt_dlp_current()
        log(f"Manual override: downloading {url}{' (forced re-download)' if force else ''}")
        success, _ = record_video(url, force=force)
    else:
        run_main("tver-downloader", process_message)
