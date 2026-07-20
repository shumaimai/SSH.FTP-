"""ランチャー + 1接続1ウィンドウ(Issue #14 段階2)のテスト。

ランチャーから接続すると、ストアを共有した独立 SessionWindow が開き、そこで
接続処理が始まることを確認する。実接続はさせない(start_connect を差し替え)。
"""
import pytest

from hashi.config import Profile


@pytest.fixture()
def launcher(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from hashi.config import config_dir
    from hashi.mainwindow import LauncherWindow, SessionWindow
    (config_dir() / "settings.json").write_text(
        '{"update_check": false}', encoding="utf-8"
    )
    before = list(SessionWindow._windows)
    w = LauncherWindow()
    yield w
    for win in list(SessionWindow._windows):
        if win not in before:
            win.session_tab = None   # shutdown を避ける
            win.close()
    w.close()


def test_connect_opens_session_window_sharing_services(launcher, monkeypatch):
    from hashi.mainwindow import SessionWindow

    started = []
    monkeypatch.setattr(SessionWindow, "start_connect",
                        lambda self: started.append(self))

    n_before = len(SessionWindow._windows)
    profile = Profile(host="h", username="u")
    launcher._connect(profile)

    assert len(SessionWindow._windows) == n_before + 1
    win = SessionWindow._windows[-1]
    assert win.profile is profile
    # ストア類は同一実体を共有
    assert win.store is launcher.store
    assert win.known_hosts is launcher.known_hosts
    assert win.credentials is launcher.credentials
    assert win.settings is launcher.settings
    # 接続が始まった / セッションメニューは接続完了まで無効
    assert started == [win]
    assert not win.m_sess.isEnabled()


def test_doubleclick_connects(launcher, monkeypatch):
    launcher.store.profiles.append(Profile(host="h", username="u"))
    launcher._reload_list()
    connected = []
    monkeypatch.setattr(type(launcher), "_connect",
                        lambda self, p: connected.append(p))
    launcher._connect_item(launcher.list.item(0))
    assert len(connected) == 1


def test_session_close_deregisters(launcher, monkeypatch):
    from hashi.mainwindow import SessionWindow

    monkeypatch.setattr(SessionWindow, "start_connect", lambda self: None)
    launcher._connect(Profile(host="h", username="u"))
    win = SessionWindow._windows[-1]
    assert win in SessionWindow._windows
    win.close()
    assert win not in SessionWindow._windows


def test_import_from_session_refreshes_launcher_list(launcher, monkeypatch):
    """セッションウィンドウでの読み込みがランチャーの一覧を更新する。"""
    from hashi.mainwindow import SessionWindow

    monkeypatch.setattr(SessionWindow, "start_connect", lambda self: None)
    launcher._connect(Profile(host="h", username="u"))
    win = SessionWindow._windows[-1]
    # ストアに直接足して _refresh_profile_lists を呼ぶ
    launcher.store.profiles.append(Profile(host="new", username="x"))
    win._refresh_profile_lists()
    labels = [launcher.list.item(i).text() for i in range(launcher.list.count())]
    assert any("new" in name or "x@new" in name for name in labels)


class _FakeSFTP:
    def listdir_attr(self, path="."): return []
    def normalize(self, path): return path or "/home/u"
    def stat(self, path): raise IOError("no such file")
    def close(self): pass


class _ModeSession:
    """SessionTab のモード別構築テスト用の最小フェイク session。"""

    class _Prof:
        username = "u"
        host = "h"
        port = 22
        initial_path = ""
        id = "u@h:22"

        def label(self):
            return "u@h"

        def id_str(self):
            return "u@h:22"

    def __init__(self):
        self.profile = self._Prof()
        self.transport = None
        self.shell_opened = 0

    def open_shell(self, cols=80, rows=24):
        self.shell_opened += 1
        class _Ch:
            def get_transport(self): return None
            def settimeout(self, *a): pass
            def recv(self, n): return b""
            def recv_ready(self): return False
            def send(self, d): pass
            def resize_pty(self, **k): pass
            def close(self): pass
            active = True
        return _Ch()

    def open_sftp(self):
        return _FakeSFTP()

    def run_sudo(self, cmd, pw): return (1, "", "")
    def is_alive(self): return True
    def close(self): pass


def _make_tab(qapp, mode):
    import pathlib
    import tempfile
    from types import SimpleNamespace

    from hashi.config import Settings
    from hashi.mainwindow import SessionTab
    st = Settings(pathlib.Path(tempfile.mkdtemp()) / "s.json")
    ctx = SimpleNamespace(
        get_sudo_password=lambda allow_prompt=True: None,
        get_login_password=lambda: None)
    return SessionTab(_ModeSession(), st, ctx, mode=mode)


def _cleanup(qapp, tab):
    """ワーカースレッドを確実に止めてから破棄する(他テストへの漏れ防止)。"""
    tab.session_log = None
    tab.shutdown()
    for _ in range(20):
        qapp.processEvents()
    tab.deleteLater()
    qapp.processEvents()


def test_session_tab_ssh_only_has_no_browser(qapp):
    tab = _make_tab(qapp, "ssh")
    assert tab.terminal is not None
    assert tab.browser is None
    assert tab.session.shell_opened == 1
    assert not tab.bt_files.isEnabled()
    assert tab.bt_term.isEnabled()
    _cleanup(qapp, tab)


def test_session_tab_sftp_only_has_no_terminal(qapp):
    tab = _make_tab(qapp, "sftp")
    assert tab.terminal is None
    assert tab.browser is not None
    assert tab.session.shell_opened == 0   # シェルを開かない
    assert not tab.bt_term.isEnabled()
    assert not tab.bt_sendpw.isEnabled()
    assert tab.toggle_session_log() is False   # ターミナルなし → 何もしない
    tab._on_password_prompt("manual")          # None ガードで落ちない
    _cleanup(qapp, tab)


def test_session_tab_both_has_terminal_and_browser(qapp):
    tab = _make_tab(qapp, "both")
    assert tab.terminal is not None and tab.browser is not None
    assert tab.bt_term.isEnabled() and tab.bt_files.isEnabled()
    _cleanup(qapp, tab)
