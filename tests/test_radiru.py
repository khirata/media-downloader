"""
Tests for らじる★らじる (NHK Radio) support in the radio worker.

Two contracts are protected here. First, routing: an NHK URL must reach the
radio worker, and a URL that merely looks like one -- an NHK World page, or an
nhk.jp *television* series -- must not. Second, naming: らじる recordings are
filed alongside Radiko's using the same {start}-{station}-{title} shape, and the
start time has to be JST, which is not what yt-dlp hands us by default.

Everything here is offline. The network-facing helpers are exercised with their
transport stubbed out.
"""
import subprocess
import sys
import types

import pytest

import worker_common as wc


# ==========================================
# Routing
# ==========================================

@pytest.mark.parametrize("url, expected", [
    # らじる: the two player forms yt-dlp matches natively
    ("https://www.nhk.or.jp/radio/player/ondemand.html?p=LG96ZW5KZ4_01_4251382", "radiru"),
    ("https://www.nhk.or.jp/radio/ondemand/detail.html?p=Z9L1V2M24L_01", "radiru"),
    ("https://www.nhk.or.jp/radionews/", "radiru"),
    # らじる: the NHK ONE pages people actually browse, with and without a slug
    ("https://www.nhk.jp/p/rs/88K7K16R6Z/", "radiru"),
    ("https://www.nhk.jp/p/culture-radio/rs/88K7K16R6Z/", "radiru"),
    ("https://www.nhk.jp/p/rs/88K7K16R6Z/episode/re/N36M62234K/", "radiru"),
    ("https://www.nhk.jp/p/culture-radio/rs/88K7K16R6Z/episode/re/N36M62234K/", "radiru"),
    ("https://www.nhk.jp/p/rs/88K7K16R6Z", "radiru"),
    # Radiko keeps working
    ("https://radiko.jp/#!/ts/FMJ/20260101120000", "radiko_ts"),
    ("https://radiko.jp/podcast/episodes/abc-123", "radiko_podcast"),
    # nhk.jp uses `ts` for television. This worker cannot download video and
    # must not swallow the URL, or it would never reach a handler that can.
    ("https://www.nhk.jp/p/nhk-news/ts/ABCDEF/", None),
    # NHK World is a different service on a different host prefix.
    ("https://www3.nhk.or.jp/nhkworld/en/shows/2049165/", None),
    ("https://tver.jp/episodes/xyz", None),
    ("not a url", None),
    (None, None),
])
def test_classify_radio_url(url, expected):
    assert wc.classify_radio_url(url) == expected


# ==========================================
# Station codes
# ==========================================

@pytest.mark.parametrize("channel, expected", [
    # What the extended-metadata call returns
    ("NHK FM・東京", "NHKFM"),
    ("NHK AM・東京", "NHKAM"),
    # What the news API returns
    ("NHK AM", "NHKAM"),
    # What yt-dlp synthesises from radio_broadcast when extended metadata 404s
    ("NHK R1", "NHKAM"),
    ("NHK FM", "NHKFM"),
    ("NHKラジオ第1", "NHKAM"),
])
def test_station_codes(channel, expected):
    assert wc.radiru_station_code(channel) == expected


def test_simulcast_resolves_to_fm():
    """
    A simulcast reports radio_broadcast "R1,FM", so both patterns match. FM has
    to win: that is what the extended-metadata path returns for the same
    programme, and the two paths must not disagree on one recording's name.
    """
    assert wc.radiru_station_code("NHK R1,FM") == "NHKFM"


@pytest.mark.parametrize("channel", [None, "", "NA"])
def test_missing_channel_falls_back_to_nhk(channel):
    assert wc.radiru_station_code(channel) == "NHK"


def test_unrecognised_channel_is_kept_rather_than_dropped():
    """An unknown service should still be identifiable in the filename."""
    assert wc.radiru_station_code("NHK ラジオ第3/実験") == "NHK ラジオ第3_実験"


# ==========================================
# Naming
# ==========================================

def test_filename_matches_the_radiko_convention():
    assert wc.radiru_filename(
        "202608310330", "NHK FM・東京", "カルチャーラジオ", "m4a"
    ) == "202608310330-NHKFM-カルチャーラジオ.m4a"


def test_description_overrides_the_title():
    assert wc.radiru_filename(
        "202608310020", "NHK AM・東京", "日曜討論", "m4a", description="政治討論"
    ) == "202608310020-NHKAM-政治討論.m4a"


def test_unsafe_characters_are_replaced():
    name = wc.radiru_filename("202608310330", "NHK FM", "特集: 月/惑星", "m4a")
    assert name == "202608310330-NHKFM-特集_ 月_惑星.m4a"


def test_long_titles_are_truncated_to_the_byte_budget():
    name = wc.radiru_filename("202608310330", "NHK FM", "あ" * 200, "m4a")
    stem = name.rsplit(".", 1)[0]
    assert len(stem.encode("utf-8")) <= wc._MAX_FILENAME_STEM_BYTES + len("202608310330-NHKFM-")


@pytest.mark.parametrize("start", [None, "", "NA"])
def test_missing_start_time_still_produces_a_usable_name(start):
    """A name without the timestamp beats refusing to file the recording."""
    assert wc.radiru_filename(start, "NHK FM", "番組", "m4a") == "NHKFM-番組.m4a"


def test_start_time_is_converted_to_jst_in_the_capture_template():
    """
    yt-dlp renders timestamps in UTC. Without the +32400 offset every programme
    airing before 09:00 JST -- most of NHK's 語学 and 深夜 lineup -- would be
    filed a day early: a 2026-08-31 03:30 JST broadcast reports upload_date
    20260830. This is the guard against anyone "simplifying" the template back
    to a plain date field.
    """
    assert "release_timestamp+32400" in wc.RADIRU_FIELDS_TEMPLATE
    assert "upload_date" not in wc.RADIRU_FIELDS_TEMPLATE
    assert "release_date" not in wc.RADIRU_FIELDS_TEMPLATE


# ==========================================
# Parsing yt-dlp's captured fields
# ==========================================

def test_parses_one_entry_per_line():
    text = (
        "/app/downloads/radiru-A_01_1.m4a\t202608310330\tNHK FM・東京\t番組ひとつ\n"
        "/app/downloads/radiru-A_01_2.m4a\t202608310400\tNHK AM\t番組ふたつ\n"
    )
    assert wc.parse_radiru_fields(text) == [
        ("/app/downloads/radiru-A_01_1.m4a", "202608310330", "NHK FM・東京", "番組ひとつ"),
        ("/app/downloads/radiru-A_01_2.m4a", "202608310400", "NHK AM", "番組ふたつ"),
    ]


def test_retry_leftovers_are_deduplicated():
    """
    yt-dlp appends to this file and run_download reruns the whole command, so an
    attempt that failed part way through leaves lines for the entries it did
    finish. The last line for a path wins.
    """
    text = (
        "/app/downloads/x.m4a\t202608310330\tNHK FM\t最初の試行\n"
        "/app/downloads/x.m4a\t202608310330\tNHK FM\t二度目の試行\n"
    )
    assert wc.parse_radiru_fields(text) == [
        ("/app/downloads/x.m4a", "202608310330", "NHK FM", "二度目の試行"),
    ]


def test_a_tab_inside_a_title_does_not_shift_the_fields():
    text = "/app/downloads/x.m4a\t202608310330\tNHK FM\t番組\tの続き\n"
    assert wc.parse_radiru_fields(text) == [
        ("/app/downloads/x.m4a", "202608310330", "NHK FM", "番組\tの続き"),
    ]


def test_malformed_lines_are_skipped_not_fatal():
    text = "garbage\n\n/app/downloads/x.m4a\t202608310330\tNHK FM\t番組\n"
    assert wc.parse_radiru_fields(text) == [
        ("/app/downloads/x.m4a", "202608310330", "NHK FM", "番組"),
    ]


# ==========================================
# nhk.jp resolution
# ==========================================

@pytest.fixture
def nhkjp_page(monkeypatch):
    """Serve a stub nhk.jp page, and pin what yt-dlp reports for the series."""
    def configure(html, episodes=()):
        class FakeResponse:
            def read(self):
                return html.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(wc.urllib.request, "urlopen", lambda *a, **k: FakeResponse())

        stdout = "".join(f"{entry_id}\t{episode_id}\n" for entry_id, episode_id in episodes)
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=""))

    return configure


# Every episode page of a series embeds that same full set of ondemand links,
# which is why the corner id can be scraped but the episode cannot.
SERIES_PAGE = (
    '<a href="/radio/player/ondemand.html?p=YRLK72JZ7Q_01_4328054">…</a>'
    '<a href="/radio/player/ondemand.html?p=YRLK72JZ7Q_01_4328821">…</a>'
)


def test_already_supported_urls_pass_through_untouched(nhkjp_page):
    url = "https://www.nhk.or.jp/radio/ondemand/detail.html?p=YRLK72JZ7Q_01"
    assert wc.resolve_radiru_url(url) == url


def test_series_url_resolves_to_the_programme(nhkjp_page):
    nhkjp_page(SERIES_PAGE)
    assert wc.resolve_radiru_url("https://www.nhk.jp/p/rs/YRLK72JZ7Q/") == (
        "https://www.nhk.or.jp/radio/ondemand/detail.html?p=YRLK72JZ7Q_01")


def test_episode_url_resolves_to_one_episode(nhkjp_page):
    """
    yt-dlp's episode_id is the nhk.jp `re/` id, which is the only thing that can
    tell two episodes of a series apart -- the page HTML cannot.
    """
    nhkjp_page(SERIES_PAGE, episodes=[
        ("YRLK72JZ7Q_01_4328054", "8MVQK66YG5"),
        ("YRLK72JZ7Q_01_4328821", "2J9Y56YWPN"),
    ])
    assert wc.resolve_radiru_url(
        "https://www.nhk.jp/p/rs/YRLK72JZ7Q/episode/re/2J9Y56YWPN/"
    ) == "https://www.nhk.or.jp/radio/player/ondemand.html?p=YRLK72JZ7Q_01_4328821"


def test_expired_episode_resolves_to_none(nhkjp_page):
    """聞き逃し carries about a week, so a stale URL must fail cleanly."""
    nhkjp_page(SERIES_PAGE, episodes=[("YRLK72JZ7Q_01_4328054", "8MVQK66YG5")])
    assert wc.resolve_radiru_url(
        "https://www.nhk.jp/p/rs/YRLK72JZ7Q/episode/re/DEADBEEF99/") is None


def test_page_without_ondemand_links_resolves_to_none(nhkjp_page):
    nhkjp_page("<html>no links here</html>")
    assert wc.resolve_radiru_url("https://www.nhk.jp/p/rs/YRLK72JZ7Q/") is None


def test_unreachable_page_resolves_to_none(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(wc.urllib.request, "urlopen", boom)
    assert wc.resolve_radiru_url("https://www.nhk.jp/p/rs/YRLK72JZ7Q/") is None


# ==========================================
# AES-128 HLS decryption guard
# ==========================================

def test_hls_crypto_present_is_silent(monkeypatch, capsys):
    # Injected rather than relying on the real package, so the test says the
    # same thing whether or not the machine running it has pycryptodomex.
    monkeypatch.setitem(sys.modules, "Cryptodome", types.ModuleType("Cryptodome"))
    assert wc.warn_if_hls_crypto_missing() is True
    assert "pycryptodomex" not in capsys.readouterr().out


def test_missing_hls_crypto_is_reported_loudly(monkeypatch, capsys):
    """
    The failure this guards is invisible everywhere else: yt-dlp falls back to
    ffmpeg, which drops audio without erroring, and the file it writes is
    internally consistent about being short — so check_truncation passes it.
    Boot is the only place the problem can be surfaced.
    """
    monkeypatch.setitem(sys.modules, "Cryptodome", None)
    assert wc.warn_if_hls_crypto_missing() is False
    out = capsys.readouterr().out
    assert "pycryptodomex" in out
    assert "WARNING" in out
