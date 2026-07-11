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


def test_alt_screen_1049_saves_and_restores_main_content(term):
    """1049 でメイン画面を保存し、解除時に元の内容へ戻す。"""
    term._on_data(b"main content")
    assert term.screen.display[0].startswith("main content")
    term._on_data(b"\x1b[?1049h")
    assert term.screen.display[0].strip() == ""
    term._on_data(b"alternate content")
    assert term.screen.display[0].startswith("alternate content")
    term._on_data(b"\x1b[?1049l")
    assert term.screen.display[0].startswith("main content")


def test_alt_screen_1049_restores_cursor(term):
    """1049 解除時に保存したカーソル位置を復元する。"""
    term._on_data(b"\x1b[5;8H")
    assert (term.screen.cursor.x, term.screen.cursor.y) == (7, 4)
    term._on_data(b"\x1b[?1049h\x1b[10;20H")
    term._on_data(b"\x1b[?1049l")
    assert (term.screen.cursor.x, term.screen.cursor.y) == (7, 4)


@pytest.mark.parametrize("mode", [47, 1047])
def test_alt_screen_non_cursor_modes_switch_buffers(term, mode):
    """47/1047 でも画面バッファを切り替え、メイン画面を復元する。"""
    term._on_data(b"main content")
    term._on_data(f"\x1b[?{mode}h".encode())
    assert term.screen.display[0].strip() == ""
    term._on_data(b"alternate content")
    assert term.screen.display[0].startswith("alternate content")
    term._on_data(f"\x1b[?{mode}l".encode())
    assert term.screen.display[0].startswith("main content")


def test_alt_screen_flag_and_ris_reset(term):
    """代替画面フラグが切り替わり、代替画面中の RIS で解除される。"""
    term._on_data(b"\x1b[?1049h")
    assert term.screen.in_alt_screen is True
    term._on_data(b"\x1bc")
    assert term.screen.in_alt_screen is False


# ---- 表示/非表示の切り替えで画面が壊れないこと (Issue #39) --------------------

class _ResizeChannel:
    """resize_pty の呼び出しを記録するだけのチャネル。"""

    def __init__(self):
        self.resizes = []

    def send(self, data: bytes):
        pass

    def resize_pty(self, width: int, height: int):
        self.resizes.append((width, height))


def test_hide_show_preserves_screen_content(qapp):
    """非表示→再表示で Qt が配達する一瞬の極小 resizeEvent を pyte へ流さない。

    デバウンスなしだと 4 桁幅への破壊的リサイズで行が「main」に切り詰められる。
    """
    from PySide6.QtWidgets import QSplitter, QWidget

    from hashi.terminal import TerminalWidget

    split = QSplitter()
    t = TerminalWidget()
    split.addWidget(t)
    split.addWidget(QWidget())
    split.resize(1000, 600)
    split.show()
    qapp.processEvents()
    t._apply_pending_grid()  # レイアウト確定後の初期サイズを適用
    ch = _ResizeChannel()
    t._channel = ch
    t._on_data(b"main content line")
    cols_before = t._cols

    t.setVisible(False)
    qapp.processEvents()
    t.setVisible(True)
    qapp.processEvents()
    t._apply_pending_grid()  # デバウンス満了相当(最終サイズだけが適用される)

    assert t.screen.display[0].startswith("main content line")
    assert t._cols == cols_before
    # 途中の極小サイズ (4 桁など) が PTY に送られていないこと
    assert all(w == cols_before for w, _h in ch.resizes)
    split.deleteLater()


def test_pending_grid_not_applied_while_hidden(qapp):
    """非表示中はデバウンスタイマーが満了しても適用しない(表示時に再計算)。"""
    from hashi.terminal import TerminalWidget

    t = TerminalWidget()
    t._on_data(b"keep me")
    cols_before = t._cols
    t._pending_grid = (4, 2)  # 非表示中に届いた極小サイズ相当
    t._apply_pending_grid()   # タイマー満了相当
    assert t._cols == cols_before
    assert t.screen.display[0].startswith("keep me")
    t.deleteLater()
