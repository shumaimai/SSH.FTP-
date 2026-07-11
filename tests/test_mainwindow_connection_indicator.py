"""接続中ページの表示テスト。"""

from hashi.config import Profile
from hashi.mainwindow import ConnectingWidget, MainWindow


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


def test_closed_connecting_tab_shows_error_dialog(qapp, monkeypatch):
    class Tabs:
        def indexOf(self, _widget):
            return -1

    class StatusBar:
        def __init__(self):
            self.messages = []

        def showMessage(self, message, timeout):
            self.messages.append((message, timeout))

    class Window:
        tabs = Tabs()
        status = StatusBar()

        def statusBar(self):
            return self.status

    window = Window()
    page = ConnectingWidget(Profile(host="example.com", username="user"))
    warnings = []
    monkeypatch.setattr(
        "hashi.mainwindow.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    MainWindow._on_connect_failed(
        window, "接続に失敗しました", page,
        Profile(host="example.com", username="user"),
    )

    assert warnings == [(window, "接続エラー", "接続に失敗しました")]
    assert window.status.messages == [("接続に失敗しました", 4000)]
    page.deleteLater()
