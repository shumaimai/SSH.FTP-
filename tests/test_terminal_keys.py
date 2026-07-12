"""ターミナルのキー入力 → エスケープ列変換 / IME / セル座標のテスト。

描画・実 PTY は絡めず、send_bytes に流れるバイト列で入力変換を固定する
(terminal / editor のテスト拡充。Issue #7 系)。
"""
import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QKeyEvent


class FakeChannel:
    def __init__(self):
        self.sent = b""

    def send(self, data: bytes):
        self.sent += data

    def resize_pty(self, width, height):
        pass


@pytest.fixture()
def term(qapp):
    from hashi.terminal import TerminalWidget
    t = TerminalWidget()
    t.screen.reset()
    t._channel = FakeChannel()
    return t


def _key(term, key, text="", mods=Qt.NoModifier):
    ev = QKeyEvent(QEvent.KeyPress, key, mods, text)
    term.keyPressEvent(ev)
    return term._channel.sent


def test_arrow_keys_send_csi(term):
    assert _key(term, Qt.Key_Up) == b"\x1b[A"
    term._channel.sent = b""
    assert _key(term, Qt.Key_Left) == b"\x1b[D"


def test_enter_tab_backspace(term):
    assert _key(term, Qt.Key_Return) == b"\r"
    term._channel.sent = b""
    assert _key(term, Qt.Key_Tab) == b"\t"
    term._channel.sent = b""
    assert _key(term, Qt.Key_Backspace) == b"\x7f"


def test_ctrl_letter_becomes_control_code(term):
    # Ctrl+C → 0x03, Ctrl+A → 0x01
    assert _key(term, Qt.Key_C, "c", Qt.ControlModifier) == b"\x03"
    term._channel.sent = b""
    assert _key(term, Qt.Key_A, "a", Qt.ControlModifier) == b"\x01"


def test_ctrl_bracket_and_space(term):
    assert _key(term, Qt.Key_BracketLeft, "[", Qt.ControlModifier) == b"\x1b"
    term._channel.sent = b""
    assert _key(term, Qt.Key_Space, " ", Qt.ControlModifier) == b"\x00"


def test_alt_prefixes_escape(term):
    # Alt+x → ESC x
    assert _key(term, Qt.Key_X, "x", Qt.AltModifier) == b"\x1bx"
    term._channel.sent = b""
    # Alt+方向キー → ESC + CSI
    assert _key(term, Qt.Key_Up, "", Qt.AltModifier) == b"\x1b\x1b[A"


def test_plain_text_is_sent_utf8(term):
    assert _key(term, Qt.Key_A, "a") == b"a"
    term._channel.sent = b""
    # 日本語 1 文字(text 経由)
    assert _key(term, 0, "あ") == "あ".encode()


def test_ctrl_shift_c_copies_not_sends(term):
    term._on_data(b"hello")
    term._sel_anchor = (0, 0)
    term._sel_end = (0, 5)
    _key(term, Qt.Key_C, "", Qt.ControlModifier | Qt.ShiftModifier)
    assert term._channel.sent == b""   # ターミナルへは送らない(ローカルコピー)


def test_modifier_only_key_is_ignored(term):
    assert _key(term, Qt.Key_Shift, "") == b""


def test_no_channel_is_noop(qapp):
    from hashi.terminal import TerminalWidget
    t = TerminalWidget()
    # _channel なしでもクラッシュしない
    t.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier, "a"))


def test_ime_commit_sends_and_preedit_stored(term):
    class _IME:
        def __init__(self, commit, pre):
            self._c, self._p = commit, pre

        def commitString(self):
            return self._c

        def preeditString(self):
            return self._p

        def accept(self):
            pass

    term.inputMethodEvent(_IME("確定", "へんかん"))
    assert term._channel.sent == "確定".encode()
    assert term._preedit == "へんかん"


def test_cell_at_clamps_to_bounds(term):
    term._cols, term._rows = 80, 24
    # 画面外(負・特大)でも範囲内へクランプ
    assert term._cell_at(QPoint(-100, -100)) == (0, 0)
    r, c = term._cell_at(QPoint(10**6, 10**6))
    assert 0 <= c <= 79 and 0 <= r <= 23
    assert (r, c) == (23, 79)
