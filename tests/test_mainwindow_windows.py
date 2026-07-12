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
    from hashi.mainwindow import LauncherWindow, SessionWindow
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
