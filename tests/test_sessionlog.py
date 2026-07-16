"""sessionlog.py のテスト (Issue #85)。"""
from __future__ import annotations

import tempfile

import pyte
import pytest

from hashi.sessionlog import SessionLog, _row_text


class _Screen:
    """テスト用の小さなスクリーン。"""

    def __init__(self, columns=80, lines=5):
        self.screen = pyte.HistoryScreen(columns, lines, history=50, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)

    def feed(self, data: bytes):
        self.stream.feed(data)


@pytest.fixture()
def log_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture()
def term(qapp):
    from hashi.terminal import TerminalWidget
    t = TerminalWidget()
    t.screen.reset()
    return t


def test_row_text_with_fullwidth():
    s = _Screen(20, 1)
    s.feed("日本語abc".encode("utf-8"))
    assert _row_text(s.screen.buffer[0], 20) == "日本語abc"


def test_session_log_writes_received_lines(log_dir):
    s = _Screen(20, 5)
    log = SessionLog("test", log_dir, enabled=True)
    s.feed(b"hello\r\nworld\r\n")
    log.write_screen(s.screen)
    log.close()
    path = log.path()
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "hello" in text
    assert "world" in text


def test_session_log_disabled_creates_no_file(log_dir):
    s = _Screen(20, 5)
    log = SessionLog("test", log_dir, enabled=False)
    s.feed(b"hello\n")
    log.write_screen(s.screen)
    log.close()
    assert log.path() is None


def test_session_log_scrolls_long_output(log_dir):
    s = _Screen(20, 3)
    log = SessionLog("test", log_dir, enabled=True)
    # 3 行の高さに対し 5 行分の出力を与えるとスクロールが発生
    s.feed(b"line0\r\nline1\r\nline2\r\nline3\r\nline4\r\n")
    log.write_screen(s.screen)
    log.flush_visible(s.screen)
    log.close()
    text = log.path().read_text(encoding="utf-8")
    for i in range(5):
        assert f"line{i}" in text


def test_session_log_does_not_log_send_input(term, log_dir):
    """入力（送信データ）はログされず、受信のみが対象。"""
    from hashi.sessionlog import SessionLog
    log = SessionLog("test", log_dir, enabled=True)
    term.set_session_log(log)
    # 送信データをチャネルへ送る（受信経路ではない）
    term._channel = type("Chan", (), {
        "send": lambda self, data: None,
        "close": lambda self: None,
    })()
    term.send_text("secret_password")
    # 受信データのみがログ対象
    term._on_data(b"prompt: ")
    term._on_data(b"output result\r\n")
    log.close()
    text = log.path().read_text(encoding="utf-8")
    assert "output result" in text
    assert "secret_password" not in text


def test_session_log_flush_visible_on_close(log_dir):
    s = _Screen(20, 5)
    log = SessionLog("test", log_dir, enabled=True)
    s.feed(b"visible line")
    log.flush_visible(s.screen)
    log.close()
    text = log.path().read_text(encoding="utf-8")
    assert "visible line" in text
