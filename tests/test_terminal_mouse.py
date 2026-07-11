"""ターミナルのマウスレポート(xterm 互換、Issue #6)のテスト。

アプリ(vim / htop 等)が ?1000/?1002/?1003 を有効にした間だけマウスイベントを
エスケープシーケンスで送る。Shift 併用はローカル操作(選択/貼り付け)に迂回。
"""
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent


class FakeChannel:
    def __init__(self):
        self.sent = b""

    def send(self, data: bytes):
        self.sent += data


@pytest.fixture()
def term(qapp):
    from hashi.terminal import TerminalWidget
    t = TerminalWidget()
    t.screen.reset()
    t._channel = FakeChannel()
    return t


def _mouse_ev(kind, x=1, y=1, button=Qt.LeftButton, buttons=None,
              modifiers=Qt.NoModifier):
    if buttons is None:
        buttons = button if kind != QEvent.MouseButtonRelease else Qt.NoButton
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y),
                       button, buttons, modifiers)


def test_modes_toggle_tracking_state(term):
    term._on_data(b"\x1b[?1000h\x1b[?1006h")
    assert term.screen.mouse_tracking == 1000
    assert term.screen.mouse_sgr is True
    term._on_data(b"\x1b[?1000l\x1b[?1006l")
    assert term.screen.mouse_tracking == 0
    assert term.screen.mouse_sgr is False


def test_sgr_click_reports_press_and_release(term):
    term._on_data(b"\x1b[?1000h\x1b[?1006h")
    term.mousePressEvent(_mouse_ev(QEvent.MouseButtonPress))
    term.mouseReleaseEvent(_mouse_ev(QEvent.MouseButtonRelease))
    assert term._channel.sent == b"\x1b[<0;1;1M\x1b[<0;1;1m"
    assert term._sel_anchor is None  # レポート中はローカル選択を開始しない


def test_legacy_click_encoding(term):
    term._on_data(b"\x1b[?1000h")
    term.mousePressEvent(_mouse_ev(QEvent.MouseButtonPress))
    term.mouseReleaseEvent(_mouse_ev(QEvent.MouseButtonRelease))
    # 押下: 32+0, 座標 32+1 / リリース: ボタン番号 3 → 32+3
    assert term._channel.sent == (
        b"\x1b[M" + bytes([32, 33, 33]) + b"\x1b[M" + bytes([35, 33, 33]))


def test_right_button_and_ctrl_modifier(term):
    term._on_data(b"\x1b[?1000h\x1b[?1006h")
    term.mousePressEvent(_mouse_ev(QEvent.MouseButtonPress,
                                   button=Qt.RightButton,
                                   modifiers=Qt.ControlModifier))
    assert term._channel.sent == b"\x1b[<18;1;1M"  # 2 (右) + 16 (Ctrl)


def test_shift_bypasses_reporting_for_local_selection(term):
    term._on_data(b"\x1b[?1000h\x1b[?1006h")
    term.mousePressEvent(_mouse_ev(QEvent.MouseButtonPress,
                                   modifiers=Qt.ShiftModifier))
    assert term._channel.sent == b""
    assert term._sel_anchor is not None  # ローカル選択が始まる


def test_1002_reports_drag_motion_once_per_cell(term):
    term._on_data(b"\x1b[?1002h\x1b[?1006h")
    term.mousePressEvent(_mouse_ev(QEvent.MouseButtonPress))
    x2 = int(term._cw * 3) + 1  # 3 セル目へ移動
    term.mouseMoveEvent(_mouse_ev(QEvent.MouseMove, x=x2))
    term.mouseMoveEvent(_mouse_ev(QEvent.MouseMove, x=x2))  # 同一セルは報告しない
    term.mouseReleaseEvent(_mouse_ev(QEvent.MouseButtonRelease, x=x2))
    assert term._channel.sent == (
        b"\x1b[<0;1;1M" b"\x1b[<32;4;1M" b"\x1b[<0;4;1m")


def test_1002_ignores_motion_without_button(term):
    term._on_data(b"\x1b[?1002h\x1b[?1006h")
    term.mouseMoveEvent(_mouse_ev(QEvent.MouseMove, buttons=Qt.NoButton))
    assert term._channel.sent == b""


def test_1003_reports_motion_without_button(term):
    term._on_data(b"\x1b[?1003h\x1b[?1006h")
    x2 = int(term._cw * 2) + 1
    term.mouseMoveEvent(_mouse_ev(QEvent.MouseMove, x=x2, buttons=Qt.NoButton))
    assert term._channel.sent == b"\x1b[<35;3;1M"  # 3 (ボタンなし) + 32 (移動)


def test_wheel_reports_when_tracking(term):
    from PySide6.QtGui import QWheelEvent

    term._on_data(b"\x1b[?1000h\x1b[?1006h")
    ev = QWheelEvent(QPointF(1, 1), QPointF(1, 1), QPoint(0, 0), QPoint(0, 120),
                     Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
    term.wheelEvent(ev)
    assert term._channel.sent == b"\x1b[<64;1;1M"


def test_wheel_scrolls_history_when_not_tracking(term):
    from PySide6.QtGui import QWheelEvent

    for i in range(60):
        term._on_data(f"line{i}\r\n".encode())
    ev = QWheelEvent(QPointF(1, 1), QPointF(1, 1), QPoint(0, 0), QPoint(0, 120),
                     Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
    term.wheelEvent(ev)
    assert term._channel.sent == b""
    assert term.screen.history.bottom  # スクロールバックが動いた


def test_reset_disables_tracking(term):
    term._on_data(b"\x1b[?1003h\x1b[?1006h")
    term._on_data(b"\x1bc")  # RIS
    assert term.screen.mouse_tracking == 0
    assert term.screen.mouse_sgr is False
