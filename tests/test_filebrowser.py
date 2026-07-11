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
    assert Browser([{"name": "notes.txt", "is_dir": False}])._terminal_target_path() == (
        "/srv/work"
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

    assert emitted == [
        "cd '/srv/user'\\''s files/project'\n",
        "'/srv/user'\\''s files/project'",
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
