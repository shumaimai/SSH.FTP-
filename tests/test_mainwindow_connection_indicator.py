"""接続中ページの表示テスト。"""

from hashi.config import Profile
from hashi.mainwindow import ConnectingWidget, SessionWindow


def test_connecting_widget_shows_indeterminate_progress(qapp):
    page = ConnectingWidget(Profile(host="example.com", username="user"))

    assert page.message.text() == "user@example.com に接続しています…"
    assert page.progress.minimum() == 0
    assert page.progress.maximum() == 0
    assert not page.progress.isHidden()

    page.deleteLater()


def test_connecting_widget_shows_connection_error(qapp):
    page = ConnectingWidget(Profile(host="example.com", username="user"))

    page.show_error("認証に失敗しました")
    assert page.message.text() == "接続に失敗しました:\n認証に失敗しました"
    assert page.progress.isHidden()

    page.show_error("")
    assert page.message.text() == "接続を中止しました"
    assert page.progress.isHidden()
    page.deleteLater()


def test_session_window_connect_failed_shows_error_on_page(qapp, monkeypatch):
    """接続失敗時、接続中ページにエラーを表示しタイトルを変える(段階2)。"""
    profile = Profile(host="example.com", username="user")

    class StatusBar:
        def __init__(self):
            self.messages = []

        def showMessage(self, message, timeout):
            self.messages.append((message, timeout))

    page = ConnectingWidget(profile)

    class Window:
        _connecting = page
        profile = Profile(host="example.com", username="user")
        _titles = []
        status = StatusBar()

        def statusBar(self):
            return self.status

        def setWindowTitle(self, t):
            self._titles.append(t)

    window = Window()
    SessionWindow._on_connect_failed(window, "認証に失敗しました")

    assert page.message.text() == "接続に失敗しました:\n認証に失敗しました"
    assert window._titles[-1] == "接続失敗: user@example.com"
    assert window.status.messages == [("接続に失敗しました", 4000)]
    page.deleteLater()
