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
    """幅縮小後もカーソルが新グリッド内に収まり、入力が表示される(#72/#100)。

    リフロー導入後は、80 文字の入力行が 40 列では 2 行に折返され、
    カーソルはその続き(3 行目の先頭)に置かれる。次の入力は画面内の
    正しい位置へ表示される。
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
    assert "Y" in term.screen.display[2]   # 折返しの続きに表示される
    assert term.screen.cursor.x <= 40


def test_wrap_tracking_marks_continuation_rows(term):
    """自動折返しで生まれた継続行を追跡する(#100)。"""
    term._pending_grid = (40, 30)
    term._apply_pending_grid()
    term._on_data(b"A" * 100)   # 40 + 40 + 20 → 行 1, 2 が継続
    assert term.screen.wrapped == {1, 2}
    # 明示的な改行は折返しではない
    term._on_data(b"\r\nB" * 1)
    assert 3 not in term.screen.wrapped


def test_reflow_grow_restores_single_logical_line(term):
    """縮小で折返した入力行が、拡大時に 1 行へ戻る(#100 の再現ケース)。"""
    term._pending_grid = (40, 30)
    term._apply_pending_grid()
    term._on_data(b"A" * 60)    # 40 + 20 に折返し
    assert term.screen.cursor.y == 1 and term.screen.cursor.x == 20

    term._pending_grid = (100, 30)
    term._apply_pending_grid()
    row0 = term.screen.display[0]
    assert row0.startswith("A" * 60)
    assert term.screen.cursor.y == 0 and term.screen.cursor.x == 60
    assert term.screen.wrapped == set()
    # 元の継続行は消えている
    assert term.screen.display[1].strip() == ""


def test_reflow_shrink_rewraps_cursor_line(term):
    """拡大状態の長い入力行が、縮小時に正しく複数行へ折返される(#100)。"""
    term._pending_grid = (100, 30)
    term._apply_pending_grid()
    term._on_data(b"B" * 80)

    term._pending_grid = (40, 30)
    term._apply_pending_grid()
    assert term.screen.display[0] == "B" * 40
    assert term.screen.display[1] == "B" * 40
    assert term.screen.cursor.y == 2 and term.screen.cursor.x == 0
    assert term.screen.wrapped == {1, 2}


def test_reflow_skips_alt_screen(term):
    """代替画面(vim 等)ではリフローしない(#100)。"""
    term._pending_grid = (60, 30)
    term._apply_pending_grid()
    term._on_data(b"\x1b[?1049h")     # 代替画面へ
    term._on_data(b"C" * 70)
    term._pending_grid = (90, 30)
    term._apply_pending_grid()        # 例外なく通ればよい(内容は維持)
    assert term.screen.in_alt_screen
    term._on_data(b"\x1b[?1049l")     # 復帰
    assert not term.screen.in_alt_screen
