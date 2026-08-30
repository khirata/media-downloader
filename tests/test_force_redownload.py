"""
Tests for the force re-download flag.

yt-dlp skips a URL whose output file already exists, and that skip is the only
thing stopping a re-published TVer episode from being fetched twice. The force
flag exists to override it, so the contract these tests protect is twofold: the
flag reaches yt-dlp when asked for, and a message body can never smuggle
anything but a boolean into the argument list.
"""
import json
import os
import subprocess
import sys

import pytest

import worker_common as wc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tver-downloader', 'src'))
import worker as tver  # noqa: E402


@pytest.fixture
def captured_argv(monkeypatch, tmp_path):
    """Run record_video without yt-dlp, returning the argv lists it built."""
    calls = []

    def run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(tver.subprocess, "run", run)
    monkeypatch.setattr(tver, "DOWNLOAD_DIR", str(tmp_path))
    return calls


def test_normal_download_adds_no_force_args():
    assert wc.build_yt_dlp_args() == wc.GLOBAL_YT_DLP_ARGS


def test_force_args_come_last_so_they_win(monkeypatch):
    # A global --download-archive must be overridden, and yt-dlp resolves a
    # conflict in favour of the later argument.
    monkeypatch.setattr(wc, "GLOBAL_YT_DLP_ARGS", ["--download-archive", "/app/downloads/.archive"])
    args = wc.build_yt_dlp_args(force=True)
    assert args[:2] == ["--download-archive", "/app/downloads/.archive"]
    assert args[2:] == wc.FORCE_DOWNLOAD_ARGS
    assert "--force-overwrites" in args


@pytest.mark.parametrize("body", [
    {"force": True},
])
def test_parse_force_accepts_only_json_true(body):
    assert wc.parse_force(body) is True


@pytest.mark.parametrize("body", [
    {},
    {"force": False},
    {"force": "true"},
    {"force": "yes"},
    {"force": 1},
    {"force": None},
    {"force": ["--exec", "rm -rf /"]},
])
def test_parse_force_rejects_everything_else(body):
    assert wc.parse_force(body) is False


def test_force_flag_reaches_yt_dlp(captured_argv):
    tver.process_message(json.dumps({"url": "https://tver.jp/episodes/abc", "force": True}))
    assert captured_argv, "yt-dlp was never invoked"
    for flag in wc.FORCE_DOWNLOAD_ARGS:
        assert flag in captured_argv[0]


def test_download_without_force_leaves_the_skip_guard_intact(captured_argv):
    tver.process_message(json.dumps({"url": "https://tver.jp/episodes/abc"}))
    assert "--force-overwrites" not in captured_argv[0]


def test_message_body_cannot_inject_yt_dlp_arguments(captured_argv):
    """The flag selects hard-coded args; no string from the body reaches argv."""
    tver.process_message(json.dumps({
        "url": "https://tver.jp/episodes/abc",
        "force": "--exec touch /tmp/pwned",
    }))
    assert captured_argv
    assert "--exec" not in captured_argv[0]
    assert not any("pwned" in arg for arg in captured_argv[0])
