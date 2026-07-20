"""ブラウザ風タブ(Issue #115)のテスト。

AppWindow が「サーバー一覧」タブを持ち、接続すると新しいタブ(SessionPage)が
開くことを確認する。実接続はさせない(start_connect を差し替え)。
"""
import pytest

from hashi.config import Profile


@pytest.fixture()
def app_win(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from hashi.config import config_dir
    from hashi.mainwindow import AppWindow, SessionPage
    (config_dir() / "settings.json").write_text(
        '{"update_check": false}', encoding="utf-8"
    )
    # 接続処理は起こさない
    monkeypatch.setattr(SessionPage, "start_connect", lambda self: None)
    before = list(SessionPage._pages)
    w = AppWindow()
    yield w
    for page in list(SessionPage._pages):
        if page not in before:
            page.session_tab = None
            page._alive_timer.stop()
            if page in SessionPage._pages:
                SessionPage._pages.remove(page)
    w.close()


def test_open_session_adds_tab_sharing_services(app_win):
    from hashi.mainwindow import SessionPage

    n_before = app_win.tabs.count()
    profile = Profile(host="h", username="u")
    page = app_win.open_session(profile)

    assert isinstance(page, SessionPage)
    assert app_win.tabs.count() == n_before + 1
    assert app_win.tabs.currentWidget() is page
    assert page.profile is profile
    # ストア類は同一実体を共有
    assert page.store is app_win.store
    assert page.known_hosts is app_win.known_hosts
    assert page.credentials is app_win.credentials
    assert page.settings is app_win.settings
    # 接続完了まではセッションメニュー無効
    assert not app_win.m_sess.isEnabled()


def test_launcher_tab_is_first_and_not_closable(app_win):
    from PySide6.QtWidgets import QTabBar

    from hashi.mainwindow import LauncherPage
    assert isinstance(app_win.tabs.widget(0), LauncherPage)
    # ランチャータブには閉じるボタンが無い
    assert app_win.tabs.tabBar().tabButton(0, QTabBar.ButtonPosition.RightSide) is None


def test_doubleclick_opens_session(app_win, monkeypatch):
    app_win.launcher.store.profiles.append(Profile(host="h", username="u"))
    app_win.launcher._reload_list()
    opened = []
    monkeypatch.setattr(app_win, "open_session",
                        lambda p, mode="both": opened.append((p, mode)))
    app_win.launcher._connect_item(app_win.launcher.list.item(0))
    assert len(opened) == 1


def test_close_tab_removes_page(app_win):
    from hashi.mainwindow import SessionPage
    page = app_win.open_session(Profile(host="h", username="u"))
    idx = app_win.tabs.indexOf(page)
    assert page in SessionPage._pages
    app_win._on_tab_close(idx)
    assert page not in SessionPage._pages


def test_import_refreshes_launcher_list(app_win):
    """読み込み等が反映される refresh_launcher。"""
    app_win.store.profiles.append(Profile(host="new", username="x"))
    app_win.refresh_launcher()
    lst = app_win.launcher.list
    labels = [lst.item(i).text() for i in range(lst.count())]
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
