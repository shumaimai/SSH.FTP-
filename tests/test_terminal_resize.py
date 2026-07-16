"""ターミナルのリサイズ整合性(Issue #72)のテスト。

pyte スクリーン・描画グリッド・PTY の 3 者のサイズが常に一致すること
(ずれるとシェルの折返しと描画が食い違い、入力位置が乱れる)を固定する。
"""
import pytest


class FakeChannel:
    def __init__(self):
        self.resized = []

    def send(self, data):
        pass

    def resize_pty(self, width, height):
        self.resized.append((width, height))


@pytest.fixture()
def term(qapp):
    from hashi.terminal import TerminalWidget
    t = TerminalWidget()
    t.screen.reset()
    t._channel = FakeChannel()
    t.show()   # offscreen でも isVisible() を立てるため
    yield t
    t.close()


def test_resize_keeps_screen_grid_pty_in_sync(term):
    term._pending_grid = (100, 30)
    term._apply_pending_grid()
    assert (term._cols, term._rows) == (100, 30)
    assert term.screen.columns == 100 and term.screen.lines == 30
    assert term._channel.resized == [(100, 30)]


def test_resize_failure_changes_nothing(term):
    """screen.resize が失敗したらグリッドも PTY も変えない(#72)。
    片方だけ新サイズにするとシェルと pyte の折返しがずれる。"""
    old = (term._cols, term._rows)
    old_screen = (term.screen.columns, term.screen.lines)

    def boom(rows, cols):
        raise RuntimeError("resize failed")

    term.screen.resize = boom
    term._pending_grid = (old[0] + 20, old[1] + 5)
    term._apply_pending_grid()
    assert (term._cols, term._rows) == old
    assert (term.screen.columns, term.screen.lines) == old_screen
    assert term._channel.resized == []


def test_resize_scrolls_to_bottom_first(term):
    """履歴スクロール中は最下段へ戻してから resize する(#72)。"""
    calls = []
    term._is_scrolled = lambda: True
    term._scroll_to_bottom = lambda: calls.append("bottom")
    orig_resize = term.screen.resize
    term.screen.resize = lambda rows, cols: (
        calls.append("resize"), orig_resize(rows, cols))
    term._pending_grid = (term._cols + 10, term._rows + 3)
    term._apply_pending_grid()
    assert calls[:2] == ["bottom", "resize"]


def test_resize_clamps_cursor_to_grid(term):
    """幅縮小後もカーソルが新グリッド内に収まり、入力が表示される(#72)。

    pyte.HistoryScreen.resize は列数を減らした際にカーソル x を
    新幅に追従させない。そのままでは次の文字が画面外に書き込まれ、
    入力位置がずれて見える。
    """
    term._pending_grid = (100, 30)
    term._apply_pending_grid()
    term._on_data(b"X" * 80)
    assert term.screen.cursor.x == 80

    term._pending_grid = (40, 30)
    term._apply_pending_grid()

    assert term.screen.columns == 40
    assert term.screen.cursor.x < 40
    term._on_data(b"Y")
    assert "Y" in term.screen.display[0]
    assert term.screen.cursor.x <= 40
