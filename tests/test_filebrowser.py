"""filebrowser.py のテスト。"""
import os


def test_quote_posix_shell_path():
    from hashi.filebrowser import _quote_posix_shell_path

    assert _quote_posix_shell_path("/srv/my files") == "'/srv/my files'"
    assert _quote_posix_shell_path("/srv/user's files") == "'/srv/user'\\''s files'"


def test_terminal_target_uses_parent_for_files():
    from hashi.filebrowser import SftpBrowser

    class Browser:
        cwd = "/srv/work"
        _terminal_target_path = SftpBrowser._terminal_target_path

        def __init__(self, entries):
            self.entries = entries

        def _selected_entries(self):
            return self.entries

    assert Browser([])._terminal_target_path() == "/srv/work"
    assert Browser([{"name": "folder", "is_dir": True}])._terminal_target_path() == (
        "/srv/work/folder"
    )
    assert Browser([{"name": "notes.txt", "is_dir": False}])._terminal_target_path(
        for_cd=True
    ) == (
        "/srv/work"
    )
    assert Browser([{"name": "notes.txt", "is_dir": False}])._terminal_target_path() == (
        "/srv/work/notes.txt"
    )


def test_terminal_path_signal_emits_shell_quoted_input(qapp):
    from PySide6.QtWidgets import QWidget

    from hashi.filebrowser import SftpBrowser

    browser = SftpBrowser.__new__(SftpBrowser)
    QWidget.__init__(browser)
    browser.cwd = "/srv/user's files"
    browser._selected_entries = lambda: [
        {"name": "project", "is_dir": True},
    ]
    emitted = []
    browser.terminal_input.connect(emitted.append)

    browser._send_terminal_path(newline=True)
    browser._send_terminal_path(newline=False)
    browser._selected_entries = lambda: [
        {"name": "notes.txt", "is_dir": False},
    ]
    browser._send_terminal_path(newline=True)
    browser._send_terminal_path(newline=False)

    assert emitted == [
        "cd '/srv/user'\\''s files/project'\n",
        "'/srv/user'\\''s files/project'",
        "cd '/srv/user'\\''s files'\n",
        "'/srv/user'\\''s files/notes.txt'",
    ]
    browser.deleteLater()


def test_external_file_monitor_emits_only_for_new_content(qapp, tmp_path):
    from hashi.filebrowser import ExternalFileMonitor

    local = tmp_path / "sample.bin"
    local.write_bytes(b"before")
    monitor = ExternalFileMonitor()
    changes = []
    monitor.changed.connect(lambda remote, path: changes.append((remote, path)))
    monitor.watch("/srv/sample.bin", str(local))

    monitor._emit_if_changed(os.path.abspath(local))
    assert changes == []

    local.write_bytes(b"after")
    scheduled = []
    monitor._schedule_check = lambda path: scheduled.append(path)
    monitor._poll_files()
    assert scheduled == [os.path.abspath(local)]

    monitor._emit_if_changed(os.path.abspath(local))
    assert changes == [("/srv/sample.bin", os.path.abspath(local))]

    monitor._emit_if_changed(os.path.abspath(local))
    assert len(changes) == 1
    monitor.stop()


def test_external_save_uses_permission_override(qapp, tmp_path):
    from hashi.filebrowser import SftpWorker

    local = tmp_path / "sample.bin"
    local.write_bytes(b"changed")
    remote = "/srv/sample.bin"

    class FakeSftp:
        def __init__(self):
            self.calls = 0

        def put(self, source, target, callback=None):
            self.calls += 1
            if self.calls == 1:
                raise PermissionError("Permission denied")
            assert source == str(local)
            assert target == remote

    class FakePermManager:
        def __init__(self):
            self.paths = []

        def with_write_access(self, path, op):
            self.paths.append(path)
            try:
                return op()
            except PermissionError:
                return op()

    worker = SftpWorker(object(), "test")
    worker.sftp = FakeSftp()
    worker.pm = FakePermManager()
    worker.perm_override = True
    results = []
    worker.external_save_result.connect(
        lambda *args: results.append(args))

    worker._job_external_save({
        "remote": remote,
        "local": str(local),
    })

    assert worker.pm.paths == [remote]
    assert worker.sftp.calls == 2
    assert results == [(remote, str(local), True, "")]


def _make_bare_browser(qapp):
    from PySide6.QtWidgets import QWidget

    from hashi.filebrowser import SftpBrowser

    class FakeMonitor:
        def __init__(self):
            self.watched = []
            self.unwatched = []

        def watch(self, remote, local):
            self.watched.append((remote, local))

        def unwatch(self, local):
            self.unwatched.append(local)

    browser = SftpBrowser.__new__(SftpBrowser)
    QWidget.__init__(browser)
    browser._external_monitor = FakeMonitor()
    browser._statuses = []
    browser._on_status = browser._statuses.append
    return browser


class _FakeSettings:
    def __init__(self, enabled):
        self.enabled = enabled

    def get(self, key):
        assert key == "external_autoupload"
        return self.enabled


def test_opened_temp_watches_when_autoupload_on(qapp, monkeypatch):
    from hashi import filebrowser

    monkeypatch.setattr(
        filebrowser.QDesktopServices, "openUrl", staticmethod(lambda url: True))
    browser = _make_bare_browser(qapp)
    browser.settings = _FakeSettings(True)

    browser._on_opened_temp("/srv/a.bin", "/tmp/a.bin")

    assert browser._external_monitor.watched == [("/srv/a.bin", "/tmp/a.bin")]
    assert "変更は自動保存" in browser._statuses[-1]
    browser.deleteLater()


def test_opened_temp_skips_watch_when_autoupload_off(qapp, monkeypatch):
    from hashi import filebrowser

    monkeypatch.setattr(
        filebrowser.QDesktopServices, "openUrl", staticmethod(lambda url: True))
    browser = _make_bare_browser(qapp)
    browser.settings = _FakeSettings(False)

    browser._on_opened_temp("/srv/b.bin", "/tmp/b.bin")

    assert browser._external_monitor.watched == []
    assert "自動保存オフ" in browser._statuses[-1]
    browser.deleteLater()


def test_opened_temp_unwatches_when_open_fails(qapp, monkeypatch):
    from hashi import filebrowser

    monkeypatch.setattr(
        filebrowser.QDesktopServices, "openUrl", staticmethod(lambda url: False))
    warnings = []
    monkeypatch.setattr(
        filebrowser.QMessageBox, "warning",
        staticmethod(lambda *args, **kwargs: warnings.append(args)))
    browser = _make_bare_browser(qapp)
    browser.settings = None  # settings なしは自動アップロード有効扱い

    browser._on_opened_temp("/srv/c.bin", "/tmp/c.bin")

    assert browser._external_monitor.watched == [("/srv/c.bin", "/tmp/c.bin")]
    assert browser._external_monitor.unwatched == ["/tmp/c.bin"]
    assert warnings
    browser.deleteLater()


def test_job_touch_creates_new_file_exclusively(qapp):
    """新規ファイル作成(Issue #64): 空作成 + 既存名は拒否。"""
    from hashi.filebrowser import SftpWorker

    class FakeSftp:
        def __init__(self, existing):
            self.existing = set(existing)
            self.opened = []

        def stat(self, path):
            if path not in self.existing:
                raise IOError("no such file")

        def open(self, path, mode):
            assert mode == "wx"
            self.opened.append(path)

            class _F:
                def close(self):
                    pass
            return _F()

    worker = SftpWorker(object(), "test")
    worker.sftp = FakeSftp(existing=[])
    statuses = []
    worker.status.connect(statuses.append)
    done = []
    worker.job_done.connect(done.append)

    worker._job_touch({"path": "/srv/new.txt"})
    assert worker.sftp.opened == ["/srv/new.txt"]
    assert done == ["touch"]
    assert any("作成しました" in s for s in statuses)

    worker.sftp = FakeSftp(existing=["/srv/new.txt"])
    import pytest
    with pytest.raises(Exception, match="既に存在します"):
        worker._job_touch({"path": "/srv/new.txt"})
    assert worker.sftp.opened == []


def test_looks_text_covers_expanded_extensions(qapp):
    from hashi.filebrowser import SftpBrowser

    for name in ("app.kt", "main.swift", "script.ps1", "note.rst",
                 "conf.editorconfig", "Cargo.lock", "id_ed25519.pub"):
        assert SftpBrowser._looks_text(name), name
    assert not SftpBrowser._looks_text("photo.png")
    assert not SftpBrowser._looks_text("archive.tar.gz")


def test_sftp_worker_reconnect_swaps_session_and_sftp(qapp):
    from hashi.filebrowser import SftpWorker

    class Ftp:
        def __init__(self, name):
            self.name = name
            self.closed = False

        def close(self):
            self.closed = True

    class Sess:
        def __init__(self, sftp):
            self.sftp = sftp

        def open_sftp(self):
            return self.sftp

    old = Sess(Ftp("old"))
    worker = SftpWorker(old, "nav")
    worker.sftp = old.sftp
    new = Sess(Ftp("new"))
    worker._reconnect(new)
    assert worker.session is new
    assert worker.sftp is new.sftp
    assert old.sftp.closed is True
