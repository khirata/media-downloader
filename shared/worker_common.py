import os
import re
import glob
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
DOWNLOAD_MAX_ATTEMPTS = max(1, int(os.environ.get('DOWNLOAD_MAX_ATTEMPTS', '3')))
DOWNLOAD_RETRY_DELAY = max(0, int(os.environ.get('DOWNLOAD_RETRY_DELAY', '10')))
YT_DLP_AUTO_UPDATE = os.environ.get('YT_DLP_AUTO_UPDATE', 'true').lower() == 'true'
YT_DLP_UPDATE_TIMEOUT = max(1, int(os.environ.get('YT_DLP_UPDATE_TIMEOUT', '300')))

sqs = boto3.client('sqs', region_name=AWS_REGION)

_UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|]')
# Leave room for extensions and yt-dlp intermediate suffixes (e.g. .f251.webm.part)
_MAX_FILENAME_STEM_BYTES = 180

# Intermediate files yt-dlp leaves behind when a download is interrupted.
# Completed outputs never carry these suffixes, so matching on them lets us
# clear the wreckage of a failed run without touching finished downloads.
_PARTIAL_DOWNLOAD_SUFFIX = re.compile(r'(?:-Frag\d+|\.ytdl|\.part|\.temp)$')

# Arguments that turn a normal download into a forced re-download. yt-dlp skips
# a URL whose output file already sits in DOWNLOAD_DIR ("has already been
# downloaded"), which is what stops the same TVer episode being fetched twice.
# --force-overwrites removes that guard, --no-continue avoids resuming a stale
# partial, and --no-download-archive covers setups that added --download-archive
# to YT_DLP_ARGS (a harmless no-op otherwise).
FORCE_DOWNLOAD_ARGS = ["--force-overwrites", "--no-continue", "--no-download-archive"]


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


def build_yt_dlp_args(force: bool = False) -> list[str]:
    """
    Build the trailing yt-dlp arguments for a download.

    Force arguments come last so they win: yt-dlp lets a later argument override
    an earlier one, and GLOBAL_YT_DLP_ARGS may itself contain a conflicting
    option such as --download-archive.
    """
    return [*GLOBAL_YT_DLP_ARGS, *(FORCE_DOWNLOAD_ARGS if force else [])]


def parse_force(data: dict) -> bool:
    """
    Read the force flag out of a decoded SQS message body.

    Deliberately strict: only a real JSON ``true`` counts. The flag is a boolean
    that selects a hard-coded argument list -- no string from a message body is
    ever passed to yt-dlp -- so anything else is treated as absent rather than
    coerced.
    """
    return data.get('force') is True


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def purge_partial_downloads(file_prefix):
    """
    Delete the intermediate files a failed yt-dlp run leaves in DOWNLOAD_DIR.

    An interrupted fragmented download leaves fragment files
    (``<name>.m4a-FragNN``) and resume state (``<name>.m4a.ytdl``) on disk.
    On the next run yt-dlp tries to resume from them and sends a Range header,
    which Radiko's CDN answers with HTTP 416 Requested Range Not Satisfiable --
    so every later attempt fails instantly until they are cleared.

    Completed outputs are deliberately left alone: runs using
    ``--download-archive`` skip an already-recorded segment and rely on finding
    the finished file still sitting in DOWNLOAD_DIR.

    Returns the number of files removed.
    """
    removed = 0
    # Matched as a prefix rather than an exact stem: a らじる series URL expands
    # into one file per episode, whose names are only known after extraction, so
    # the caller can name a common prefix but not each stem. Widening is safe
    # because the suffix check below -- not the glob -- is what authorises a
    # delete, and the worker handles one SQS message at a time.
    for path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{file_prefix}*")):
        if not _PARTIAL_DOWNLOAD_SUFFIX.search(path):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            log(f"Could not remove partial download {path}: {e}")
    if removed:
        log(f"Removed {removed} partial download file(s) for {file_prefix}")
    return removed


def run_download(cmd, file_prefix, label):
    """
    Run a yt-dlp command, clearing partial downloads before every attempt and
    retrying up to DOWNLOAD_MAX_ATTEMPTS times.

    Radiko's CDN returns intermittent 5xx responses that abort an otherwise
    healthy download, so a bounded retry recovers the recording instead of
    losing it to a momentary blip. Recordings are small, which makes
    re-downloading a whole segment cheap.

    Returns True if yt-dlp exited 0 within the attempt budget.
    """
    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        purge_partial_downloads(file_prefix)
        try:
            subprocess.run(cmd, check=True)
            return True
        except subprocess.CalledProcessError as e:
            if attempt >= DOWNLOAD_MAX_ATTEMPTS:
                log(f"Error downloading {label} after {attempt} attempt(s): {e}")
                purge_partial_downloads(file_prefix)
                return False
            log(f"Error downloading {label} (attempt {attempt}/{DOWNLOAD_MAX_ATTEMPTS}): {e}")
            if DOWNLOAD_RETRY_DELAY:
                log(f"Retrying in {DOWNLOAD_RETRY_DELAY}s...")
                time.sleep(DOWNLOAD_RETRY_DELAY)
    return False


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


# ==========================================
# らじる★らじる (NHK Radio) URL handling
# ==========================================

# yt-dlp's NhkRadiru extractor only matches these two www.nhk.or.jp player URLs.
_RADIRU_ONDEMAND_RE = re.compile(
    r'^https?://www\.nhk\.or\.jp/radio/(?:player/ondemand|ondemand/detail)\.html\?p=', re.I)
_RADIRU_NEWS_RE = re.compile(r'^https?://www\.nhk\.or\.jp/radionews/?(?:$|[?#])', re.I)

# The NHK ONE site people actually browse. `rs` is deliberate: nhk.jp uses `ts`
# for television series, and those must not be routed to the radio worker.
_RADIRU_NHKJP_RE = re.compile(
    r'^https?://www\.nhk\.jp/p/(?:[^/?#]+/)?rs/(?P<series>[\da-zA-Z]+)'
    r'(?:/episode/re/(?P<episode>[\da-zA-Z]+))?(?:[/?#]|$)', re.I)

_RADIKO_PODCAST_RE = re.compile(r'^https?://(?:www\.)?radiko\.jp/podcast/', re.I)
_RADIKO_TS_RE = re.compile(r'^https?://(?:www\.)?radiko\.jp/#!/ts/', re.I)

# NHK names the service in prose, and the wording differs between yt-dlp's two
# metadata paths: the extended-metadata call yields "NHK FM・東京" / "NHK AM・東京",
# while the fallback built from the series API's radio_broadcast field yields
# "NHK FM" / "NHK R1" / "NHK R1,FM". The news API contributes "NHK AM" again.
# Map them all onto a short code so filenames stay parallel with Radiko's
# station ids.
#
# FM is tested first on purpose: a simulcast reports radio_broadcast "R1,FM",
# and resolving that to NHKFM agrees with what the extended metadata returns for
# the same programme (checked against NHKのど自慢).
#
# There is no NHKR2 code. ラジオ第2 was folded into FM in 2025, and it could not
# be reached even for old recordings -- 聞き逃し only carries about a week.
_RADIRU_STATION_PATTERNS = (
    ('NHKFM', re.compile(r'FM')),
    ('NHKAM', re.compile(r'AM|ラジオ第1|R1')),
)

# yt-dlp prints this for a field the extractor did not populate.
_YT_DLP_NA = 'NA'


def classify_radio_url(url):
    """
    Identify which download path a queued radio URL belongs to.

    Returns 'radiko_podcast', 'radiru', 'radiko_ts', or None when the URL is not
    something the radio worker knows how to fetch.
    """
    if not isinstance(url, str):
        return None
    if _RADIKO_PODCAST_RE.match(url):
        return 'radiko_podcast'
    if _RADIKO_TS_RE.match(url):
        return 'radiko_ts'
    if (_RADIRU_ONDEMAND_RE.match(url)
            or _RADIRU_NEWS_RE.match(url)
            or _RADIRU_NHKJP_RE.match(url)):
        return 'radiru'
    return None


def radiru_station_code(channel):
    """
    Short station code for a らじる channel name, mirroring Radiko station ids.

    Falls back to the sanitised channel name so an unrecognised service still
    produces a usable filename rather than silently losing the station.
    """
    if not channel or channel == _YT_DLP_NA:
        return 'NHK'
    for code, pattern in _RADIRU_STATION_PATTERNS:
        if pattern.search(channel):
            return code
    return truncate_filename(sanitize_description(channel))


def radiru_filename(start_jst, channel, title, ext, description=None):
    """
    Build the final filename for a らじる episode.

    Mirrors the Radiko time-shift convention -- {start}-{station}-{title}.{ext},
    with start as 12 JST digits -- so recordings from both sources sort together
    in the same folder.

    The start time also keeps a series download collision-free: every episode
    carries its own broadcast time, so even a shared `description` yields
    distinct names.
    """
    parts = []
    if start_jst and start_jst != _YT_DLP_NA:
        parts.append(start_jst)
    parts.append(radiru_station_code(channel))

    label = description or (title if title != _YT_DLP_NA else '') or ''
    label = truncate_filename(sanitize_description(label))
    if label:
        parts.append(label)

    return f"{'-'.join(parts)}.{ext}"


# Fields captured from each completed download, tab-delimited on one line.
#
# release_timestamp is the broadcast start -- the direct analogue of Radiko's
# start_time. yt-dlp renders timestamps in UTC, so +32400 shifts it to JST.
# Without that offset every programme airing before 09:00 JST would be filed a
# day early, which is most of NHK's 語学 and 深夜 lineup. (yt-dlp's upload_date
# and release_date have the same UTC problem and cannot be corrected in a
# template, which is why neither is used here.)
RADIRU_FIELDS_TEMPLATE = (
    "after_move:%(filepath)s\t%(release_timestamp+32400>%Y%m%d%H%M)s"
    "\t%(channel)s\t%(title)s"
)


def parse_radiru_fields(text):
    """
    Parse the tab-delimited lines yt-dlp wrote for RADIRU_FIELDS_TEMPLATE.

    Returns a list of (path, start_jst, channel, title) tuples.

    Deduplicates by path, keeping the last line seen for each: yt-dlp appends to
    this file, and run_download reruns the whole command on retry, so an attempt
    that failed part way through leaves lines behind for the entries it did
    finish.
    """
    entries = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) < 4:
            log(f"Skipping malformed yt-dlp field line: {line!r}")
            continue
        # Rejoin any trailing fields: a title is free text and could contain a tab.
        path, start, channel = parts[0], parts[1], parts[2]
        title = '\t'.join(parts[3:])
        entries[path] = (path, start, channel, title)
    return list(entries.values())


def _fetch_radiru_corner(page_url, series_id):
    """
    Read the corner id out of an nhk.jp programme page.

    A らじる programme is addressed as <series>_<corner>, but an nhk.jp URL
    carries only the series id. The page embeds ondemand.html links that supply
    the missing half. Returns None on any failure; never raises.
    """
    try:
        req = urllib.request.Request(
            page_url, headers={"User-Agent": "Mozilla/5.0 (compatible; media-downloader/1.0)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        log(f"Failed to fetch nhk.jp page {page_url}: {e}")
        return None

    match = re.search(rf'ondemand\.html\?p={re.escape(series_id)}_([\da-zA-Z]+)', html)
    if not match:
        log(f"No ondemand link found on {page_url}")
        return None
    return match.group(1)


def _find_radiru_headline(series_url, episode_id):
    """
    Map an nhk.jp episode id to the headline id that addresses it on らじる.

    Scraping cannot do this: every episode page of a series lists that same full
    set of ondemand links. yt-dlp's episode_id *is* the nhk.jp `re/` id, so a
    simulate pass over the series resolves it. Returns None if the episode is not
    among those currently available (聞き逃し expires after about a week).
    """
    cmd = ["yt-dlp", "--ignore-config", "-s", "--print", "%(id)s\t%(episode_id)s", series_url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"Failed to list episodes for {series_url}: {e}")
        return None

    for line in result.stdout.splitlines():
        entry_id, _, entry_episode = line.strip().partition('\t')
        if entry_episode == episode_id:
            return entry_id.rsplit('_', 1)[-1]

    log(f"Episode {episode_id} is not among the available episodes of {series_url}")
    return None


def resolve_radiru_url(url):
    """
    Rewrite an nhk.jp programme URL into the らじる form yt-dlp understands.

    URLs already on www.nhk.or.jp are returned unchanged. Returns None when an
    nhk.jp URL cannot be resolved, so the caller can fail the job cleanly rather
    than handing yt-dlp a URL no extractor matches.
    """
    match = _RADIRU_NHKJP_RE.match(url)
    if not match:
        return url

    series_id, episode_id = match.group('series'), match.group('episode')

    corner_id = _fetch_radiru_corner(url, series_id)
    if not corner_id:
        return None

    series_url = f"https://www.nhk.or.jp/radio/ondemand/detail.html?p={series_id}_{corner_id}"
    if not episode_id:
        return series_url

    headline_id = _find_radiru_headline(series_url, episode_id)
    if not headline_id:
        return None
    return f"https://www.nhk.or.jp/radio/player/ondemand.html?p={series_id}_{corner_id}_{headline_id}"


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


def yt_dlp_version():
    """Return the yt-dlp version string, or None if it cannot be determined."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log(f"Could not determine yt-dlp version: {e}")
        return None
    return result.stdout.strip() or None


def _yt_dlp_packages():
    """
    yt-dlp plus any installed yt-dlp plugin distributions (e.g. yt-dlp-rajiko).

    Plugins hook into yt-dlp internals, so upgrading yt-dlp while leaving a
    plugin behind can break the very extractor the plugin provides. Discovering
    them by name prefix keeps the two in step without each worker having to
    declare its own plugin list.
    """
    packages = {"yt-dlp"}
    try:
        from importlib.metadata import distributions
        for dist in distributions():
            name = dist.metadata.get("Name") or ""
            if name.startswith("yt-dlp-"):
                packages.add(name)
    except Exception as e:
        log(f"Could not enumerate yt-dlp plugins, upgrading yt-dlp alone: {e}")
    return sorted(packages)


def ensure_yt_dlp_current():
    """
    Upgrade yt-dlp (and its plugins) before the worker starts accepting jobs.

    YouTube reworks its player and streaming protocol every few weeks, and each
    change breaks extraction until yt-dlp ships a fix. The image only ever
    contains whichever release existed when it was built, so a long-running
    container silently rots: downloads begin failing with extractor errors such
    as "The page needs to be reloaded." while nothing in this repo has changed.
    Refreshing at start makes `docker compose restart` enough to recover, with
    no image rebuild.

    Upgrade failures are deliberately non-fatal. A worker that cannot reach
    PyPI is still more useful running the version baked into the image than not
    running at all.
    """
    before = yt_dlp_version()

    if not YT_DLP_AUTO_UPDATE:
        log(f"yt-dlp auto-update disabled; running {before or 'unknown version'}")
        return

    packages = _yt_dlp_packages()
    log(f"Upgrading {', '.join(packages)} (currently {before or 'unknown version'})...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade", *packages],
            capture_output=True, text=True, check=True, timeout=YT_DLP_UPDATE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log(f"yt-dlp upgrade failed, continuing with {before or 'unknown version'}: {e}")
        return

    after = yt_dlp_version()
    if after and before and after != before:
        log(f"yt-dlp upgraded {before} -> {after}")
    else:
        log(f"yt-dlp up to date at {after or 'unknown version'}")


def run_main(worker_name, process_message_fn):
    """SQS long-poll loop. Delegates message handling to process_message_fn."""
    if not SQS_QUEUE_URL:
        log("Error: SQS_QUEUE_URL is not set.")
        sys.exit(1)

    ensure_yt_dlp_current()

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
