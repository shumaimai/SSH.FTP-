"""sudo ワンタップ送信ボタンのテスト。

自動送信は廃止(リモートはプロンプトを偽装できる)。sudo プロンプト検知時は
ボタンを表示し、送る判断は常に人間が行う。SessionTab の該当メソッドを
最小フェイクの self に対して呼ぶ(実 SSH 接続は不要)。
"""
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton, QWidget

from hashi.mainwindow import SessionTab


class _SecretCtx:
    def __init__(self, pw="saved-sudo"):
        self.pw = pw

    def get_sudo_password(self):
        return self.pw


class _Terminal:
    def __init__(self):
        self.sent = []

    def send_password(self, pw):
        self.sent.append(pw)


class _Settings(dict):
    def get(self, key, default=None):  # Settings 互換
        return dict.get(self, key, default)


@pytest.fixture()
def tab(qapp):
    """SessionTab の必要属性だけ持つフェイク。"""
    host = QWidget()  # ボタンの親

    class T:
        settings = _Settings({"sudo_autofill": True})
        secret_ctx = _SecretCtx()

        def __init__(self):
            self.terminal = _Terminal()
            self.flashes = []
            self._last_autofill_ts = 0.0
            self._sudo_btn = QPushButton("送信", host)
            self._sudo_btn.setVisible(False)
            self._sudo_btn_timer = QTimer(host)
            self._sudo_btn_timer.setSingleShot(True)
            self._host = host  # GC 防止

        def width(self):
            return 800

        def _flash(self, text, warn=False):
            self.flashes.append((text, warn))

        _on_password_prompt = SessionTab._on_password_prompt
        _show_sudo_button = SessionTab._show_sudo_button
        _send_sudo_password = SessionTab._send_sudo_password

    return T()


def test_sudo_prompt_shows_button_without_sending(tab):
    """検知しただけでは何も送らない。ボタンが出るだけ。"""
    tab._on_password_prompt("sudo")
    assert tab._sudo_btn.isVisibleTo(tab._sudo_btn.parentWidget())
    assert tab.terminal.sent == []


def test_button_click_sends_saved_password_once(tab):
    tab._on_password_prompt("sudo")
    tab._send_sudo_password()  # クリック相当
    assert tab.terminal.sent == ["saved-sudo"]
    assert not tab._sudo_btn.isVisibleTo(tab._sudo_btn.parentWidget())


def test_reprompt_within_cooldown_hides_button(tab):
    """送信直後の再プロンプト = パスワード違い。同じものを再送させない。"""
    tab._on_password_prompt("sudo")
    tab._send_sudo_password()
    tab._on_password_prompt("sudo")  # 8 秒以内の再検知
    assert not tab._sudo_btn.isVisibleTo(tab._sudo_btn.parentWidget())
    assert tab.terminal.sent == ["saved-sudo"]  # 1 回だけ
    assert any(w for _t, w in tab.flashes)      # 警告が出ている


def test_setting_off_shows_hint_only(tab):
    tab.settings["sudo_autofill"] = False
    tab._on_password_prompt("sudo")
    assert not tab._sudo_btn.isVisibleTo(tab._sudo_btn.parentWidget())
    assert tab.terminal.sent == []


def test_password_prompt_never_shows_button(tab):
    """password/passphrase は別ホストの可能性があるためボタンも出さない。"""
    tab._on_password_prompt("password")
    tab._on_password_prompt("passphrase")
    assert not tab._sudo_btn.isVisibleTo(tab._sudo_btn.parentWidget())
    assert tab.terminal.sent == []


def test_no_saved_password_warns(tab):
    tab.secret_ctx = _SecretCtx(pw=None)
    tab._on_password_prompt("sudo")
    tab._send_sudo_password()
    assert tab.terminal.sent == []
    assert any(w for _t, w in tab.flashes)


def test_manual_send_uses_sudo_then_login_password(tab):
    """🔑 ボタン / メニューの手動送信 (Issue #40)。sudo → ログインの順で使う。"""
    tab.secret_ctx.get_login_password = lambda: "login-pw"
    tab._on_password_prompt("manual")
    assert tab.terminal.sent == ["saved-sudo"]

    tab.secret_ctx = _SecretCtx(pw=None)
    tab.secret_ctx.get_login_password = lambda: "login-pw"
    tab._on_password_prompt("manual")
    assert tab.terminal.sent == ["saved-sudo", "login-pw"]


def test_manual_send_without_any_password_warns(tab):
    tab.secret_ctx = _SecretCtx(pw=None)
    tab.secret_ctx.get_login_password = lambda: None
    tab._on_password_prompt("manual")
    assert tab.terminal.sent == []
    assert any(w for _t, w in tab.flashes)
