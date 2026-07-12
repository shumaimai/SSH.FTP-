"""複数ウィンドウ接続(Issue #14 段階1)のテスト。

新しいウィンドウがストア(プロファイル / 既知ホスト / 認証情報 / 設定)を
共有して開き、そこで接続処理が始まることを確認する。実接続はさせない
(_connect_profile を差し替え)。
"""
import pytest

from hashi.config import Profile


@pytest.fixture()
def main_window(qapp, tmp_path, monkeypatch):
    # 設定ディレクトリを隔離(実ユーザーの config を触らない)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from hashi.mainwindow import MainWindow
    before = list(MainWindow._windows)
    w = MainWindow()
    yield w
    # 後始末: このテストで開いたウィンドウを閉じる
    for win in list(MainWindow._windows):
        if win not in before:
            win.close()


def test_new_window_shares_services_and_connects(main_window, monkeypatch):
    from hashi.mainwindow import MainWindow

    calls = []
    monkeypatch.setattr(MainWindow, "_connect_profile",
                        lambda self, p: calls.append((self, p)))

    n_before = len(MainWindow._windows)
    profile = Profile(host="h", username="u")
    main_window._connect_in_new_window(profile)

    assert len(MainWindow._windows) == n_before + 1
    child = MainWindow._windows[-1]
    assert child is not main_window
    # ストア類は同一実体を共有
    assert child.store is main_window.store
    assert child.known_hosts is main_window.known_hosts
    assert child.credentials is main_window.credentials
    assert child.settings is main_window.settings
    # 新ウィンドウ側で接続が始まった
    assert calls and calls[-1][0] is child and calls[-1][1] is profile


def test_close_deregisters_window(main_window):
    from hashi.mainwindow import MainWindow

    services = main_window._services
    child = MainWindow(services=services)
    assert child in MainWindow._windows
    child.close()
    assert child not in MainWindow._windows


def test_ctrl_doubleclick_opens_new_window(main_window, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from hashi.mainwindow import MainWindow

    main_window.store.profiles.append(Profile(host="h", username="u"))
    main_window._reload_list()

    new_win = []
    normal = []
    monkeypatch.setattr(MainWindow, "_connect_in_new_window",
                        lambda self, p: new_win.append(p))
    monkeypatch.setattr(MainWindow, "_connect_profile",
                        lambda self, p: normal.append(p))

    item = main_window.list.item(0)

    monkeypatch.setattr(QApplication, "keyboardModifiers",
                        staticmethod(lambda: Qt.ControlModifier))
    main_window._connect_item(item)
    assert len(new_win) == 1 and not normal

    monkeypatch.setattr(QApplication, "keyboardModifiers",
                        staticmethod(lambda: Qt.NoModifier))
    main_window._connect_item(item)
    assert len(normal) == 1
