"""新バージョン通知(Issue #101)のテスト。"""
import json
from io import BytesIO
from unittest.mock import patch

import pytest


def test_parse_version_and_is_newer(qapp):
    from hashi.updatecheck import _is_newer, _parse_version

    assert _parse_version("0.6.0") == (0, 6, 0)
    assert _parse_version("v0.7.0") == (0, 7, 0)
    assert _parse_version("1.2.3-beta") == (1, 2, 3)
    assert _is_newer("0.6.0", "0.7.0") is True
    assert _is_newer("0.6.0", "0.6.0") is False
    assert _is_newer("0.7.0", "0.6.0") is False


def test_worker_emits_for_new_version(qapp):
    from hashi.updatecheck import UpdateCheckWorker

    payload = json.dumps(
        {"tag_name": "v0.7.0", "html_url": "https://example.com/release"}
    ).encode("utf-8")

    def fake_urlopen(req, **kwargs):
        return BytesIO(payload)

    received = []
    with patch("hashi.updatecheck.urllib.request.urlopen", fake_urlopen):
        worker = UpdateCheckWorker(current_version="0.6.0")
        worker.new_version.connect(lambda tag, url: received.append((tag, url)))
        worker.run()

    assert received == [("v0.7.0", "https://example.com/release")]


def test_worker_fails_silently(qapp):
    from hashi.updatecheck import UpdateCheckWorker

    with patch(
        "hashi.updatecheck.urllib.request.urlopen",
        side_effect=OSError("offline"),
    ):
        worker = UpdateCheckWorker(current_version="0.6.0")
        worker.new_version.connect(lambda *_: pytest.fail("シグナルが発火した"))
        worker.run()


def test_worker_ignores_older_version(qapp):
    from hashi.updatecheck import UpdateCheckWorker

    payload = json.dumps(
        {"tag_name": "v0.5.0", "html_url": "https://example.com/old"}
    ).encode("utf-8")

    received = []
    with patch(
        "hashi.updatecheck.urllib.request.urlopen", return_value=BytesIO(payload)
    ):
        worker = UpdateCheckWorker(current_version="0.6.0")
        worker.new_version.connect(lambda *_: received.append(True))
        worker.run()

    assert not received


def test_settings_default_update_check_is_true():
    from hashi.config import Settings

    s = Settings()
    assert s.get("update_check") is True


def test_launcher_banner_show_and_close(qapp, tmp_config, monkeypatch):
    from hashi.config import KnownHosts, ProfileStore, Settings
    from hashi.credentials import CredentialStore
    from hashi.mainwindow import LauncherWindow
    from hashi.snippets import SnippetStore

    settings = Settings()
    settings.set("update_check", False)
    services = {
        "store": ProfileStore(),
        "known_hosts": KnownHosts(),
        "settings": settings,
        "credentials": CredentialStore(),
        "snippets": SnippetStore(),
    }
    w = LauncherWindow(services)
    assert w._update_banner.isHidden()

    w._show_update_banner("v0.7.0", "https://example.com/release")
    assert not w._update_banner.isHidden()
    assert "v0.7.0" in w._update_label.text()
    assert "ダウンロード" in w._update_label.text()
    w.close()
