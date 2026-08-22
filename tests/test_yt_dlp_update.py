"""
Tests for the yt-dlp freshness check that runs before a worker takes jobs.

The worker upgrades yt-dlp on start because sites break extraction every few
weeks and the image only ever holds the release it was built with. That upgrade
touches the network, so the contract these tests protect is: it may fail in any
way it likes, and the worker still starts.
"""
import subprocess

import pytest

import worker_common as wc


@pytest.fixture
def fake_version(monkeypatch):
    """Pin what `yt-dlp --version` reports, returning the list of pip argv seen."""
    pip_calls = []
    versions = iter(())

    def run(cmd, *args, **kwargs):
        if cmd[:1] == ["yt-dlp"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=next(versions) + "\n", stderr="")
        pip_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def configure(*reported):
        nonlocal versions
        versions = iter(reported)
        monkeypatch.setattr(subprocess, "run", run)
        return pip_calls

    return configure


def test_upgrades_and_reports_the_new_version(fake_version, monkeypatch, capsys):
    monkeypatch.setattr(wc, "YT_DLP_AUTO_UPDATE", True)
    pip_calls = fake_version("2025.10.14", "2026.08.19")

    wc.ensure_yt_dlp_current()

    assert len(pip_calls) == 1
    assert pip_calls[0][1:5] == ["-m", "pip", "install", "--no-cache-dir"]
    assert "--upgrade" in pip_calls[0]
    assert "yt-dlp" in pip_calls[0]
    assert "2025.10.14 -> 2026.08.19" in capsys.readouterr().out


def test_reports_no_change_when_already_current(fake_version, monkeypatch, capsys):
    monkeypatch.setattr(wc, "YT_DLP_AUTO_UPDATE", True)
    fake_version("2026.08.19", "2026.08.19")

    wc.ensure_yt_dlp_current()

    out = capsys.readouterr().out
    assert "up to date at 2026.08.19" in out
    assert "->" not in out


def test_disabled_skips_pip_entirely(fake_version, monkeypatch, capsys):
    monkeypatch.setattr(wc, "YT_DLP_AUTO_UPDATE", False)
    pip_calls = fake_version("2025.10.14")

    wc.ensure_yt_dlp_current()

    assert pip_calls == []
    assert "auto-update disabled" in capsys.readouterr().out


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(1, "pip"),
    subprocess.TimeoutExpired("pip", 300),
    OSError("pip is missing"),
])
def test_upgrade_failure_never_stops_the_worker(failure, monkeypatch, capsys):
    """A worker that cannot reach PyPI is still useful on the baked-in version."""
    monkeypatch.setattr(wc, "YT_DLP_AUTO_UPDATE", True)

    def run(cmd, *args, **kwargs):
        if cmd[:1] == ["yt-dlp"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="2025.10.14\n", stderr="")
        raise failure

    monkeypatch.setattr(subprocess, "run", run)

    wc.ensure_yt_dlp_current()  # must not raise

    assert "continuing with 2025.10.14" in capsys.readouterr().out


def test_missing_yt_dlp_reports_unknown_rather_than_raising(monkeypatch):
    def run(cmd, *args, **kwargs):
        raise FileNotFoundError("yt-dlp")

    monkeypatch.setattr(subprocess, "run", run)

    assert wc.yt_dlp_version() is None


def test_plugins_are_upgraded_alongside_yt_dlp(monkeypatch):
    """A plugin left on an old release breaks the extractor it provides."""
    class Dist:
        def __init__(self, name):
            self.metadata = {"Name": name}

    monkeypatch.setattr(
        "importlib.metadata.distributions",
        lambda: [Dist("boto3"), Dist("yt-dlp"), Dist("yt-dlp-rajiko")],
    )

    assert wc._yt_dlp_packages() == ["yt-dlp", "yt-dlp-rajiko"]
