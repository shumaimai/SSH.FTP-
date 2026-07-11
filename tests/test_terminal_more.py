"""terminal.py のロジック部テスト(Issue #7)。

描画・IME は手動確認のままだが、選択コピー(全角含む)・送信変換・
プロンプト重複抑止・タイトル通知はここで自動化する。
"""
import pytest
from PySide6.QtGui import QGuiApplication


class FakeChannel:
    """send() を記録するだけのチャネル。"""

    def __init__(self):
        self.sent = b""

    def send(self, data: bytes):
        self.sent += data


@pytest.fixture()
def term(qapp):
    from hashi.terminal import TerminalWidget
    t = TerminalWidget()
    t.screen.reset()
    return t


def _select(t, anchor, end):
    t._sel_anchor = anchor
    t._sel_end = end


def test_copy_selection_ascii(term):
    term._on_data(b"hello world")
    _select(term, (0, 0), (0, 10))
    term.copy_selection()
    assert QGuiApplication.clipboard().text() == "hello world"


def test_copy_selection_fullwidth(term):
    """全角(幅 2)を含む行でも文字が欠けず・重複せずコピーされる。"""
    term._on_data("日本語abc".encode("utf-8"))
    _select(term, (0, 0), (0, 8))  # 2*3 + 3 = 9 セル
    term.copy_selection()
    assert QGuiApplication.clipboard().text() == "日本語abc"


def test_copy_selection_multiline_rstrip(term):
    """複数行選択。行末の埋めスペースは落ちる。"""
    term._on_data(b"first\r\nsecond line")
    _select(term, (0, 0), (1, 10))
    term.copy_selection()
    assert QGuiApplication.clipboard().text() == "first\nsecond line"


def test_sel_range_normalizes_reverse_drag(term):
    """上向きドラッグ(anchor が後ろ)でも (start, end) は昇順になる。"""
    _select(term, (2, 5), (0, 3))
    lo, hi = term._sel_range()
    assert lo == 0 * term._cols + 3
    assert hi == 2 * term._cols + 5


def test_send_text_converts_newline_to_cr(term):
    """貼り付け経路では LF が CR に変換される(端末の改行は CR)。"""
    ch = FakeChannel()
    term._channel = ch
    term.send_text("ls\npwd\n")
    assert ch.sent == b"ls\rpwd\r"


def test_send_password_appends_newline_and_resets(term):
    ch = FakeChannel()
    term._channel = ch
    term._last_pw_prompt = "0:Password:"
    term.send_password("s3cret")
    assert ch.sent == b"s3cret\n"
    assert term._last_pw_prompt == ""


def test_prompt_not_emitted_twice_for_same_prompt(term):
    """同じプロンプトを 2 回検知しても通知は 1 回(重複通知防止)。"""
    hits = []
    term.password_prompt.connect(lambda k: hits.append(k))
    term._on_data(b"[sudo] password for tester: ")
    term._detect_password_prompt()
    term._detect_password_prompt()
    assert hits == ["sudo"]


def test_prompt_emitted_again_after_send_password(term):
    """パスワード送信後に同じプロンプトが再表示されたら改めて通知される
    (パスワード誤り検知の前提となる挙動)。"""
    hits = []
    term.password_prompt.connect(lambda k: hits.append(k))
    term._on_data(b"[sudo] password for tester: ")
    term._detect_password_prompt()
    term.send_password("wrong")   # 検知フラグがリセットされる
    term._detect_password_prompt()
    assert hits == ["sudo", "sudo"]


def test_title_changed_via_osc(term):
    """OSC 0 シーケンスでタイトル変更が通知される。"""
    titles = []
    term.title_changed.connect(lambda s: titles.append(s))
    term._on_data(b"\x1b]0;tester@host: ~\x07")
    term._flush()
    assert titles == ["tester@host: ~"]


def test_send_bytes_without_channel_is_noop(term):
    """未接続(channel=None)でも送信系が落ちない。"""
    term.send_text("ls\n")
    term.send_password("x")


def test_bracketed_paste_mode_set_and_reset(term):
    """CSI ?2004 h/l でブラケットペーストモードが切り替わる。"""
    assert term.screen.bracketed_paste is False
    term._on_data(b"\x1b[?2004h")
    assert term.screen.bracketed_paste is True
    term._on_data(b"\x1b[?2004l")
    assert term.screen.bracketed_paste is False


def test_bracketed_paste_wraps_clipboard(term):
    """ブラケットペースト有効時、貼り付けを ESC[200~ / ESC[201~ で囲み、
    LF を CR に変換しない。
    """
    ch = FakeChannel()
    term._channel = ch
    term._on_data(b"\x1b[?2004h")
    QGuiApplication.clipboard().setText("def foo():\n    pass")
    term.paste_clipboard()
    assert ch.sent == b"\x1b[200~def foo():\n    pass\x1b[201~"


def test_paste_clipboard_without_bracketed_paste(term):
    """ブラケットペースト無効時、貼り付けは send_text 経路で LF→CR される。"""
    ch = FakeChannel()
    term._channel = ch
    QGuiApplication.clipboard().setText("ls\npwd")
    term.paste_clipboard()
    assert ch.sent == b"ls\rpwd"


def test_bracketed_paste_reset_on_ris(term):
    """RIS (ESC c) でブラケットペーストモードもリセットされる。"""
    term._on_data(b"\x1b[?2004h")
    assert term.screen.bracketed_paste is True
    term._on_data(b"\x1bc")
    assert term.screen.bracketed_paste is False
