"""接続中ページの表示テスト。"""

from hashi.config import Profile
from hashi.mainwindow import ConnectingWidget


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
