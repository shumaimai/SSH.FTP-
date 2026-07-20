"""接続中ページの表示テスト。"""

from hashi.config import Profile
from hashi.mainwindow import ConnectingWidget, SessionPage


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


def test_session_page_connect_failed_shows_error_on_page(qapp):
    """接続失敗時、接続中ページにエラーを表示しタイトル/ステータスを更新(#115)。"""
    profile = Profile(host="example.com", username="user")
    page = ConnectingWidget(profile)

    class FakePage:
        _connecting = page
        profile = Profile(host="example.com", username="user")

        def __init__(self):
            self.titles = []
            self.statuses = []

        def _set_title(self, t):
            self.titles.append(t)

        def _status(self, m):
            self.statuses.append(m)

    fp = FakePage()
    SessionPage._on_connect_failed(fp, "認証に失敗しました")

    assert page.message.text() == "接続に失敗しました:\n認証に失敗しました"
    assert fp.titles[-1] == "接続失敗: user@example.com"
    assert fp.statuses == ["接続に失敗しました"]
    page.deleteLater()
