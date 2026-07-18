"""ターミナルウィジェット。

paramiko の対話シェルチャネルの出力を pyte でエミュレートし、
セルグリッドを自前描画する。xterm-256color として動作。

対応: 256色/truecolor, 太字/下線/反転, スクロールバック,
     マウス選択コピー(自動コピー), 中クリック/Ctrl+Shift+V 貼り付け,
     日本語IME入力, 全角(East Asian Wide)描画, PTY リサイズ
"""
from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict, deque
from functools import partial

import pyte
from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
)
from PySide6.QtWidgets import QMenu, QWidget
from wcwidth import wcwidth

from . import themes
from .dialogs import SnippetVariablesDialog
from .snippets import expand_snippet

logger = logging.getLogger(__name__)

# ---- カラーパレット (One Half Dark ベース) ---------------------------------
DEFAULT_FG = QColor("#dcdfe4")
DEFAULT_BG = QColor("#1b1f27")
CURSOR_COLOR = QColor("#dcdfe4")
SELECTION_BG = QColor("#3e4b63")

NAMED_COLORS = {
    "black": "#3b4048",
    "red": "#e06c75",
    "green": "#98c379",
    "brown": "#e5c07b",     # pyte は SGR 33 (yellow) を "brown" と呼ぶ
    "yellow": "#e5c07b",
    "blue": "#61afef",
    "magenta": "#c678dd",
    "cyan": "#56b6c2",
    "white": "#dcdfe4",
    "brightblack": "#5c6370",
    "brightred": "#e06c75",
    "brightgreen": "#98c379",
    "brightbrown": "#e5c07b",
    "brightyellow": "#e5c07b",
    "brightblue": "#61afef",
    "brightmagenta": "#c678dd",
    "brightcyan": "#56b6c2",
    "brightwhite": "#ffffff",
}
_color_cache: dict[str, QColor] = {}


def resolve_color(name, default: QColor) -> QColor:
    """pyte の色表現 (名前 or 16進6桁) を QColor へ。"""
    if not name or name == "default":
        return default
    c = _color_cache.get(name)
    if c is not None:
        return c
    hexval = NAMED_COLORS.get(name)
    if hexval is None and len(name) == 6:
        try:
            int(name, 16)
            hexval = "#" + name
        except ValueError:
            hexval = None
    c = QColor(hexval) if hexval else default
    _color_cache[name] = c
    return c


# URL 検出 (第一弾: 行内 http/https。行折返しは未対応)
_URL_RE = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)
_URL_TRIM_CHARS = ".,;:!?"


class _ChannelBridge(QObject):
    """受信スレッド → GUI スレッドへのシグナル橋渡し。"""
    data_received = Signal(object)   # bytes
    channel_closed = Signal()


# 特殊キー → エスケープシーケンス
_KEYMAP = {
    Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B",
    Qt.Key_Right: b"\x1b[C", Qt.Key_Left: b"\x1b[D",
    Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F",
    Qt.Key_PageUp: b"\x1b[5~", Qt.Key_PageDown: b"\x1b[6~",
    Qt.Key_Insert: b"\x1b[2~", Qt.Key_Delete: b"\x1b[3~",
    Qt.Key_F1: b"\x1bOP", Qt.Key_F2: b"\x1bOQ",
    Qt.Key_F3: b"\x1bOR", Qt.Key_F4: b"\x1bOS",
    Qt.Key_F5: b"\x1b[15~", Qt.Key_F6: b"\x1b[17~",
    Qt.Key_F7: b"\x1b[18~", Qt.Key_F8: b"\x1b[19~",
    Qt.Key_F9: b"\x1b[20~", Qt.Key_F10: b"\x1b[21~",
    Qt.Key_F11: b"\x1b[23~", Qt.Key_F12: b"\x1b[24~",
    Qt.Key_Escape: b"\x1b",
    Qt.Key_Backspace: b"\x7f",
    Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r",
    Qt.Key_Tab: b"\t",
}


class _TerminalScreen(pyte.HistoryScreen):
    """pyte.HistoryScreen を拡張し、xterm 互換のプライベートモードを追加する。

    ブラケットペースト (?2004) 有効時は、貼り付けテキストを
    ESC[200~ ... ESC[201~ で挟んで送信できるようになる。

    代替画面中に PTY がリサイズされた場合、保存中のメイン画面は
    リサイズされないため、復帰時に内容が一部切り詰められることがある。
    """

    _BRACKETED_PASTE_MODE = 2004
    _ALT_SCREEN_MODES = frozenset({47, 1047, 1049})
    # マウスレポート: 1000=クリック / 1002=ボタン押下中の移動も / 1003=常に移動も
    _MOUSE_MODES = frozenset({1000, 1002, 1003})
    _MOUSE_SGR_MODE = 1006   # SGR 拡張座標 (223 桁制限なし)

    def __init__(self, *args, **kwargs):
        self.bracketed_paste = False
        self.in_alt_screen = False
        self.mouse_tracking = 0   # 0=無効 / 1000 / 1002 / 1003
        self.mouse_sgr = False
        self._main_buffer = None
        self._main_history = None
        self._saved_cursor = None
        # 自動折返し(DECAWM)で生まれた「前行の継続」行の集合(Issue #100)。
        # リサイズ時にカーソルの論理行を再折返しするために追跡する。
        self.wrapped: set[int] = set()
        self._in_draw = False
        super().__init__(*args, **kwargs)

    def reset(self):
        self.bracketed_paste = False
        self.in_alt_screen = False
        self.mouse_tracking = 0
        self.mouse_sgr = False
        self._main_buffer = None
        self._main_history = None
        self._saved_cursor = None
        self.wrapped = set()
        super().reset()

    # ---- 折返し行の追跡 (Issue #100) ---------------------------------------
    def draw(self, data):
        # draw 中の linefeed だけが「自動折返し」。明示的な LF と区別する
        self._in_draw = True
        try:
            super().draw(data)
        finally:
            self._in_draw = False

    def linefeed(self):
        super().linefeed()
        if self._in_draw:
            self.wrapped.add(self.cursor.y)

    def index(self):
        bottom = self.margins.bottom if self.margins else self.lines - 1
        scrolls = self.cursor.y == bottom
        super().index()
        if scrolls and self.wrapped:
            self.wrapped = {y - 1 for y in self.wrapped if y >= 1}

    def reverse_index(self):
        top = self.margins.top if self.margins else 0
        scrolls = self.cursor.y == top
        super().reverse_index()
        if scrolls and self.wrapped:
            self.wrapped = {y + 1 for y in self.wrapped if y + 1 < self.lines}

    def erase_in_display(self, how=0, *args, **kwargs):
        super().erase_in_display(how, *args, **kwargs)
        if how in (2, 3):
            self.wrapped.clear()

    # ---- リフロー第 1 段 (Issue #100): カーソルの論理行のみ ----------------
    def resize(self, lines=None, columns=None):
        """リサイズ時、カーソルがいる論理行(継続行含む)を新しい幅で
        再折返しする。

        pyte はリフロー非対応のため、シェル(readline)が「入力行は N 行に
        折返している」前提で行うカーソル相対移動と画面の実態がずれ、
        縮小→拡大で入力位置が崩れていた(実機 #96/#100)。全画面のリフローは
        行わず、崩れの実害が出る入力行(カーソル行)だけを対象にする。
        """
        lines = lines or self.lines
        columns = columns or self.columns
        old_lines, old_cols = self.lines, self.columns
        plan = None
        if not self.in_alt_screen and columns != old_cols:
            plan = self._extract_cursor_logical_line(old_cols)
        super().resize(lines, columns)
        if plan is None:
            self.wrapped.clear()
            return
        cells, cursor_off, start, span = plan
        # 行数が減った場合は上から切り詰められる(pyte 仕様)ので追従
        start -= max(0, old_lines - lines)
        self.wrapped.clear()
        if start < 0:
            return
        self._relayout_cursor_line(cells, cursor_off, start, span)

    def _extract_cursor_logical_line(self, old_cols):
        """カーソルの論理行を (セル列, カーソルオフセット, 開始行, 行数) で返す。"""
        start = min(self.cursor.y, self.lines - 1)
        while start > 0 and start in self.wrapped:
            start -= 1
        end = min(self.cursor.y, self.lines - 1)
        while (end + 1) in self.wrapped and end + 1 < self.lines:
            end += 1
        cells = []
        cursor_off = 0
        for y in range(start, end + 1):
            row = self.buffer[y]
            if y < end:
                used = old_cols            # 継続がある行は幅いっぱい使っている
            else:
                used = 0
                for x in range(old_cols - 1, -1, -1):
                    ch = row[x]
                    if ch.data and ch.data != " ":
                        used = x + 1
                        break
                if y == self.cursor.y:
                    used = max(used, min(self.cursor.x, old_cols))
            if y == self.cursor.y:
                cursor_off = len(cells) + min(self.cursor.x, old_cols)
            for x in range(used):
                cells.append(row[x])
        return cells, cursor_off, start, end - start + 1

    def _relayout_cursor_line(self, cells, cursor_off, start, old_span):
        new_cols = self.columns
        need = max(len(cells), cursor_off + 1)
        rows_needed = max(1, -(-need // new_cols))
        if start + rows_needed > self.lines:
            return  # 収まらない稀ケースは何もしない(従来挙動 + clamp に任せる)
        for y in range(start, min(start + max(old_span, rows_needed), self.lines)):
            self.buffer.pop(y, None)
        for i, cell in enumerate(cells):
            self.buffer[start + i // new_cols][i % new_cols] = cell
        for y in range(start + 1, start + rows_needed):
            self.wrapped.add(y)
        self.cursor.y = start + cursor_off // new_cols
        self.cursor.x = cursor_off % new_cols
        self.dirty.update(range(start, self.lines))

    def set_mode(self, *modes, private=False):
        if private and self._ALT_SCREEN_MODES.intersection(modes):
            self._enter_alt_screen(save_cursor=1049 in modes)
        if private and self._BRACKETED_PASTE_MODE in modes:
            self.bracketed_paste = True
        if private:
            for m in modes:
                if m in self._MOUSE_MODES:
                    self.mouse_tracking = m
                elif m == self._MOUSE_SGR_MODE:
                    self.mouse_sgr = True
        super().set_mode(*modes, private=private)

    def reset_mode(self, *modes, private=False):
        if private and self._ALT_SCREEN_MODES.intersection(modes):
            self._exit_alt_screen(restore_cursor=1049 in modes)
        if private and self._BRACKETED_PASTE_MODE in modes:
            self.bracketed_paste = False
        if private:
            for m in modes:
                if m in self._MOUSE_MODES:
                    self.mouse_tracking = 0
                elif m == self._MOUSE_SGR_MODE:
                    self.mouse_sgr = False
        super().reset_mode(*modes, private=private)

    def _enter_alt_screen(self, save_cursor):
        if self.in_alt_screen:
            return
        self.in_alt_screen = True
        if save_cursor:
            self._saved_cursor = (self.cursor.x, self.cursor.y)
        self._main_buffer = self.buffer
        self._main_history = self.history
        self._main_wrapped = set(self.wrapped)
        self.wrapped = set()
        self.buffer = defaultdict(self._main_buffer.default_factory)
        self.history = self.history._replace(
            top=deque(maxlen=self.history.size),
            bottom=deque(maxlen=self.history.size),
            position=self.history.size,
        )
        self.dirty.update(range(self.lines))
        self.cursor_position()

    def _exit_alt_screen(self, restore_cursor):
        if not self.in_alt_screen:
            return
        self.in_alt_screen = False
        if self._main_buffer is not None:
            self.buffer = self._main_buffer
        if self._main_history is not None:
            self.history = self._main_history
        self.wrapped = getattr(self, "_main_wrapped", set())
        self._main_wrapped = set()
        self._main_buffer = None
        self._main_history = None
        self.dirty.update(range(self.lines))
        if restore_cursor and self._saved_cursor is not None:
            self.cursor.x, self.cursor.y = self._saved_cursor
            self.ensure_hbounds()
            self.ensure_vbounds()
        self._saved_cursor = None


class TerminalWidget(QWidget):
    """1 セッション分のターミナル画面。attach(channel) で使用開始。"""

    session_closed = Signal()
    title_changed = Signal(str)
    password_prompt = Signal(str)   # sudo/パスワードプロンプト検知 (種別文字列)

    # パスワード入力を求めるプロンプトのパターン (行末付近で一致)
    _PW_PATTERNS = [
        (re.compile(r"\[sudo\] password for .+:\s*$"), "sudo"),
        (re.compile(r"\bpassword for .+:\s*$", re.I), "sudo"),
        (re.compile(r"'s password:\s*$"), "password"),
        (re.compile(r"(^|\s)password:\s*$", re.I), "password"),
        (re.compile(r"enter passphrase for .+:\s*$", re.I), "passphrase"),
    ]

    def __init__(self, parent=None, font_size: int = 11,
                 right_click_paste: bool = True,
                 theme: str = "", font_family: str = ""):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_InputMethodEnabled, True)  # 日本語 IME
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.IBeamCursor)

        self._channel = None
        self._bridge = _ChannelBridge()
        self._bridge.data_received.connect(self._on_data)
        self._bridge.channel_closed.connect(self._on_closed)
        self._reader: threading.Thread | None = None
        self._closed = False

        self._cols, self._rows = 80, 24
        self.screen = _TerminalScreen(self._cols, self._rows, history=5000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)

        self._font_size = font_size
        self._font_family = font_family
        self._build_fonts()
        self.set_theme(theme)

        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setInterval(30)  # ~33fps で再描画をまとめる
        self._timer.timeout.connect(self._flush)
        self._timer.start()

        # リサイズのデバウンス。表示/非表示の切り替え中に Qt が一瞬だけ
        # 極小サイズの resizeEvent を配達することがあり(Issue #39)、
        # そのまま pyte へ流すと画面内容が数文字に切り詰められて戻らない
        # (pyte の resize は破壊的)。最終サイズだけを適用する。
        self._pending_grid: tuple[int, int] | None = None
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(80)
        self._resize_timer.timeout.connect(self._apply_pending_grid)

        self._sel_anchor: tuple[int, int] | None = None  # (row, col)
        self._sel_end: tuple[int, int] | None = None
        self._mouse_pressed_code: int | None = None   # レポート送信済みの押下ボタン
        self._last_mouse_cell: tuple[int, int] | None = None
        self._preedit = ""  # IME 変換中文字列
        self._last_title = ""
        self._right_click_paste = right_click_paste
        self._snippet_store = None  # 右クリックメニューのスニペット用
        self._url_cache: dict[int, tuple[tuple[str, ...], list[tuple[int, int, str]]]] = {}
        self._last_pw_prompt = ""  # 直近に通知したプロンプト(重複通知防止)
        self._session_log = None  # SessionLog (受信出力の自動保存)

    # ---- フォント/セル寸法 --------------------------------------------------
    def _build_fonts(self):
        f = QFont()
        families = ["Consolas", "Cascadia Mono", "MS Gothic", "Monospace"]
        if self._font_family:
            families.insert(0, self._font_family)  # 設定フォント優先(#78)
        f.setFamilies(families)
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(self._font_size)
        self._font = f
        self._font_bold = QFont(f)
        self._font_bold.setBold(True)
        fm = QFontMetricsF(f)
        self._cw = max(1.0, fm.horizontalAdvance("M"))
        self._chh = max(1.0, fm.height())

    # ---- 配色テーマ (Issue #78) ----------------------------------------------
    def set_theme(self, name: str = ""):
        """配色テーマを適用する(未知の名前は既定へフォールバック)。"""
        t = themes.get_theme(name)
        self._c_fg = QColor(t["foreground"])
        self._c_bg = QColor(t["background"])
        self._c_cursor = QColor(t["cursor"])
        self._c_sel = QColor(t["selection"])
        self._ansi = {k: QColor(v) for k, v in t["ansi"].items()}
        self._theme_cache: dict[str, QColor] = {}
        self._c_link = self._resolve("blue", self._c_fg)
        self._dirty = True
        self.update()

    def _resolve(self, name, default: QColor) -> QColor:
        """pyte の色表現 (名前 or 16進6桁) を現在テーマの QColor へ。"""
        if not name or name == "default":
            return default
        c = self._theme_cache.get(name)
        if c is not None:
            return c
        c = self._ansi.get(name)
        if c is None and len(name) == 6:
            try:
                int(name, 16)
                c = QColor("#" + name)
            except ValueError:
                c = None
        c = c or default
        self._theme_cache[name] = c
        return c

    def set_font_family(self, family: str):
        """等幅フォントを差し替える(設定の即時反映用、#99)。"""
        if family == self._font_family:
            return
        self._font_family = family
        self._build_fonts()
        self._recalc_grid()
        self.update()

    def set_font_size(self, size: int):
        self._font_size = max(6, min(32, size))
        self._build_fonts()
        self._recalc_grid()
        self.update()

    def font_size(self) -> int:
        return self._font_size

    def _parse_urls(self, row: int) -> list[tuple[int, int, str]]:
        """指定行のテキストから URL 範囲を返す (start_col, end_col, url)。"""

        def make_key():
            line = self.screen.buffer[row]
            return tuple(line[c].data for c in range(self._cols))

        line = self.screen.buffer[row]
        cached = self._url_cache.get(row)
        key = make_key()
        if cached is not None and cached[0] == key:
            return cached[1]

        chars: list[str] = []
        start_col: list[int] = []
        end_col: list[int] = []
        col = 0
        try:
            while col < self._cols:
                ch = line[col]
                if ch.data:
                    chars.append(ch.data)
                    start_col.append(col)
                    ww = wcwidth(ch.data)
                    if ww and ww > 1:
                        col += ww
                    else:
                        col += 1
                    end_col.append(col)
                else:
                    col += 1
        except Exception:
            logger.debug("URL 検出中の行アクセスに失敗", exc_info=True)
            return []

        s = "".join(chars)
        ranges: list[tuple[int, int, str]] = []
        for m in _URL_RE.finditer(s):
            start_str, end_str = m.start(), m.end()
            while end_str > start_str and s[end_str - 1] in _URL_TRIM_CHARS:
                end_str -= 1
            if end_str <= start_str:
                continue
            sc = start_col[start_str]
            ec = end_col[end_str - 1]
            ranges.append((sc, ec, s[start_str:end_str]))

        self._url_cache[row] = (key, ranges)
        return ranges

    def _url_at(self, row: int, col: int) -> str | None:
        """指定セルが URL に含まれる場合、その URL 文字列を返す。"""
        for sc, ec, url in self._parse_urls(row):
            if sc <= col < ec:
                return url
        return None

    def set_session_log(self, log):
        """受信出力の追記ログを設定/解除する。"""
        self._session_log = log

    # ---- チャネル接続 -------------------------------------------------------
    def attach(self, channel):
        self._channel = channel
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._recalc_grid()

    def _read_loop(self):
        ch = self._channel
        try:
            while True:
                data = ch.recv(8192)
                if not data:
                    break
                self._bridge.data_received.emit(data)
        except Exception:
            logger.debug("ターミナル受信ループが例外で終了 (チャネルクローズ)",
                         exc_info=True)
        self._bridge.channel_closed.emit()

    def _on_data(self, data: bytes):
        try:
            self.stream.feed(data)
        except Exception:
            logger.debug("未対応シーケンスを無視して継続", exc_info=True)
        if self._session_log is not None:
            self._session_log.write_screen(self.screen)
        self._dirty = True

    def _on_closed(self):
        self._closed = True
        self._dirty = True
        self.session_closed.emit()

    def _flush(self):
        if self._dirty:
            self._dirty = False
            self.update()
            if self.screen.title != self._last_title:
                self._last_title = self.screen.title
                self.title_changed.emit(self._last_title)
            self._detect_password_prompt()

    def _detect_password_prompt(self):
        """カーソル行がパスワード入力待ちかを判定して通知。"""
        if self._closed or self._is_scrolled():
            return
        try:
            cur = self.screen.cursor
            line = self.screen.buffer[cur.y]
            text = "".join(line[c].data or " " for c in range(cur.x)).rstrip()
        except Exception:
            logger.debug("パスワードプロンプト検知の行取得に失敗", exc_info=True)
            return
        if not text or not text.endswith(":"):
            self._last_pw_prompt = ""
            return
        for pat, kind in self._PW_PATTERNS:
            if pat.search(text):
                sig = f"{cur.y}:{text}"
                if sig != self._last_pw_prompt:
                    self._last_pw_prompt = sig
                    self.password_prompt.emit(kind)
                return
        self._last_pw_prompt = ""

    def send_password(self, secret: str):
        """パスワードを送信 (末尾に改行)。プロンプト検知フラグをリセット。"""
        self._last_pw_prompt = ""
        self.send_bytes((secret + "\n").encode("utf-8"))

    # ---- 送信 ---------------------------------------------------------------
    def send_bytes(self, data: bytes):
        if self._channel is None or self._closed:
            return
        try:
            self._channel.send(data)
        except Exception:
            logger.debug("ターミナルへの送信に失敗", exc_info=True)

    def send_text(self, text: str):
        self.send_bytes(text.replace("\n", "\r").encode("utf-8"))

    def paste_clipboard(self):
        text = QGuiApplication.clipboard().text()
        if not text:
            return
        if getattr(self.screen, "bracketed_paste", False):
            # ブラケットペースト中は改行を変換せず、元のテキストをそのまま囲んで送信
            self.send_bytes(b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~")
        else:
            self.send_text(text)

    # ---- グリッド/リサイズ ----------------------------------------------------
    def _recalc_grid(self):
        """サイズ変更を予約する(実適用はデバウンス後の _apply_pending_grid)。"""
        cols = max(4, int(self.width() / self._cw))
        rows = max(2, int(self.height() / self._chh))
        self._pending_grid = (cols, rows)
        self._resize_timer.start()

    def _apply_pending_grid(self):
        if self._pending_grid is None:
            return
        if not self.isVisible():
            return  # 非表示中は適用しない(showEvent で再計算する)
        cols, rows = self._pending_grid
        self._pending_grid = None
        if (cols, rows) != (self._cols, self._rows):
            # 履歴スクロール中の pyte resize はカーソル/行の再配置が乱れる
            # ことがあるので、必ず最下段へ戻してから適用する(Issue #72)
            if self._is_scrolled():
                self._scroll_to_bottom()
            try:
                self.screen.resize(rows, cols)  # pyte は (lines, columns) の順
            except Exception:
                # スクリーンを変えられなかったのに描画グリッドや PTY だけ
                # 新サイズにすると、シェル(新幅で折返す)と pyte(旧幅)が
                # ずれて入力位置が乱れる(Issue #72)。何も変えずに揃える。
                logger.warning("screen.resize に失敗 (グリッド/PTY を維持)",
                               exc_info=True)
                self._dirty = True
                return
            # pyte の HistoryScreen.resize は幅縮小時にカーソル行の
            # 再配置が不完全なことがある。スクリーン範囲に収めることで、
            # その後の描画・入力位置がグリッド外にずれるのを防ぐ (#72)
            self.screen.ensure_hbounds()
            self.screen.ensure_vbounds()
            self._cols, self._rows = cols, rows
            self._url_cache.clear()
            if self._channel is not None and not self._closed:
                try:
                    self._channel.resize_pty(width=cols, height=rows)
                except Exception:
                    logger.debug("resize_pty に失敗 (無視)", exc_info=True)
        self._dirty = True

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._recalc_grid()

    def showEvent(self, ev):
        super().showEvent(ev)
        # 非表示中に届いたサイズ変更は捨てているので、表示時点の実サイズで取り直す
        self._recalc_grid()

    def sizeHint(self) -> QSize:
        return QSize(int(self._cw * 80), int(self._chh * 24))

    # ---- スクロールバック ------------------------------------------------------
    def _is_scrolled(self) -> bool:
        try:
            return bool(self.screen.history.bottom)
        except Exception:
            logger.debug("スクロール位置の取得に失敗", exc_info=True)
            return False

    def _scroll_to_bottom(self):
        guard = 0
        while self._is_scrolled() and guard < 2000:
            self.screen.next_page()
            guard += 1
        self._dirty = True

    def wheelEvent(self, ev):
        if self._mouse_report_active(ev.modifiers()):
            # ホイールは 64 (上) / 65 (下)。押下イベントのみでリリースなし
            code = 64 if ev.angleDelta().y() > 0 else 65
            self._send_mouse(code, ev.position().toPoint(), pressed=True)
            ev.accept()
            return
        if ev.angleDelta().y() > 0:
            self.screen.prev_page()
        else:
            self.screen.next_page()
        self._dirty = True
        ev.accept()

    # ---- キー入力 --------------------------------------------------------------
    def event(self, ev):
        # ターミナルにフォーカスがある間は、打鍵をウィンドウレベルの
        # ショートカット(ファイルブラウザの Backspace=上へ 等)に横取り
        # させない。ShortcutOverride を accept すると通常のキーイベントとして
        # このウィジェットに届く(実機で Backspace が効かなくなる事故の再発防止)。
        if ev.type() == QEvent.ShortcutOverride:
            ev.accept()
            return True
        return super().event(ev)

    def keyPressEvent(self, ev):
        if self._channel is None:
            return
        key = ev.key()
        mods = ev.modifiers()

        # コピー/ペースト (Ctrl+Shift+C / V)
        if mods & Qt.ControlModifier and mods & Qt.ShiftModifier:
            if key == Qt.Key_C:
                self.copy_selection()
                return
            if key == Qt.Key_V:
                self.paste_clipboard()
                return

        # Shift+PgUp/PgDn はスクロールバック
        if mods & Qt.ShiftModifier and key in (Qt.Key_PageUp, Qt.Key_PageDown):
            if key == Qt.Key_PageUp:
                self.screen.prev_page()
            else:
                self.screen.next_page()
            self._dirty = True
            return

        # 入力したら最下部へ戻す
        if self._is_scrolled():
            self._scroll_to_bottom()

        # Ctrl+英字 → 制御コード
        if mods & Qt.ControlModifier and not (mods & Qt.AltModifier):
            if Qt.Key_A <= key <= Qt.Key_Z:
                self.send_bytes(bytes([key - Qt.Key_A + 1]))
                return
            if key == Qt.Key_Space or key == Qt.Key_At:
                self.send_bytes(b"\x00")
                return
            if key == Qt.Key_BracketLeft:
                self.send_bytes(b"\x1b")
                return

        seq = _KEYMAP.get(key)
        if seq is not None:
            if mods & Qt.AltModifier:
                seq = b"\x1b" + seq
            self.send_bytes(seq)
            return

        text = ev.text()
        if text:
            data = text.encode("utf-8")
            if mods & Qt.AltModifier:
                data = b"\x1b" + data
            self.send_bytes(data)
        # それ以外(修飾キー単独など)は無視

    def focusNextPrevChild(self, next_) -> bool:
        return False  # Tab キーをターミナルへ渡す

    # ---- IME ---------------------------------------------------------------
    def inputMethodEvent(self, ev):
        commit = ev.commitString()
        if commit:
            if self._is_scrolled():
                self._scroll_to_bottom()
            self.send_text(commit)
        self._preedit = ev.preeditString()
        self._dirty = True
        ev.accept()

    def inputMethodQuery(self, query):
        if query == Qt.ImCursorRectangle:
            c = self.screen.cursor
            return QRect(
                int(c.x * self._cw), int(c.y * self._chh),
                int(self._cw), int(self._chh),
            )
        if query == Qt.ImEnabled:
            return True
        return super().inputMethodQuery(query)

    # ---- マウス選択 -----------------------------------------------------------
    def _cell_at(self, pos: QPoint) -> tuple[int, int]:
        col = max(0, min(self._cols - 1, int(pos.x() / self._cw)))
        row = max(0, min(self._rows - 1, int(pos.y() / self._chh)))
        return row, col

    # ---- マウスレポート (xterm 互換。Issue #6) --------------------------------
    # アプリ (vim / htop 等) が ?1000/?1002/?1003 を有効にしている間は
    # マウスイベントをエスケープシーケンスでリモートへ送る。
    # Shift 併用時はレポートを迂回してローカル操作 (選択/貼り付け) にする (xterm 流)。

    _QT_MOUSE_BTN = {Qt.LeftButton: 0, Qt.MiddleButton: 1, Qt.RightButton: 2}

    def _mouse_report_active(self, modifiers) -> bool:
        return (getattr(self.screen, "mouse_tracking", 0) != 0
                and not (modifiers & Qt.ShiftModifier)
                and self._channel is not None and not self._closed)

    @staticmethod
    def _mouse_modifier_bits(modifiers) -> int:
        bits = 0
        if modifiers & Qt.AltModifier:
            bits += 8
        if modifiers & Qt.ControlModifier:
            bits += 16
        return bits

    def _send_mouse(self, btn_code: int, pos: QPoint, pressed: bool,
                    motion: bool = False):
        row, col = self._cell_at(pos)
        x, y = col + 1, row + 1
        b = btn_code + (32 if motion else 0)
        if getattr(self.screen, "mouse_sgr", False):
            suffix = "M" if pressed else "m"
            self.send_bytes(f"\x1b[<{b};{x};{y}{suffix}".encode("ascii"))
        else:
            # 旧形式: リリースはボタン番号 3。座標は 223 まで (超えたら送らない)
            if not pressed:
                b = (b & ~0b11) | 3
            if x > 223 or y > 223:
                return
            self.send_bytes(b"\x1b[M" + bytes([32 + b, 32 + x, 32 + y]))

    def mousePressEvent(self, ev):
        self.setFocus()
        if ev.button() == Qt.LeftButton and (ev.modifiers() & Qt.ControlModifier):
            row, col = self._cell_at(ev.position().toPoint())
            url = self._url_at(row, col)
            if url:
                QDesktopServices.openUrl(QUrl(url, QUrl.ParsingMode.TolerantMode))
                ev.accept()
                return
        if self._mouse_report_active(ev.modifiers()) \
                and ev.button() in self._QT_MOUSE_BTN:
            self._mouse_pressed_code = (self._QT_MOUSE_BTN[ev.button()]
                                        + self._mouse_modifier_bits(ev.modifiers()))
            self._last_mouse_cell = self._cell_at(ev.position().toPoint())
            self._send_mouse(self._mouse_pressed_code,
                             ev.position().toPoint(), pressed=True)
            return
        if ev.button() == Qt.LeftButton:
            self._sel_anchor = self._cell_at(ev.position().toPoint())
            self._sel_end = self._sel_anchor
            self._dirty = True
        elif ev.button() == Qt.MiddleButton:
            self.paste_clipboard()
        elif ev.button() == Qt.RightButton:
            # PuTTY 流: 右クリックで貼り付け (設定 ON かつ Shift 非押下時)
            if self._right_click_paste and not (ev.modifiers() & Qt.ShiftModifier):
                self.paste_clipboard()

    def mouseMoveEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            row, col = self._cell_at(ev.position().toPoint())
            if self._url_at(row, col):
                self.setCursor(Qt.PointingHandCursor)
                return
            self.setCursor(Qt.IBeamCursor)
        elif not (ev.buttons() & Qt.LeftButton):
            self.setCursor(Qt.IBeamCursor)
        tracking = getattr(self.screen, "mouse_tracking", 0)
        if self._mouse_report_active(ev.modifiers()) and tracking >= 1002:
            pressed = getattr(self, "_mouse_pressed_code", None)
            if tracking == 1002 and pressed is None:
                return  # 1002 はボタン押下中の移動のみ
            cell = self._cell_at(ev.position().toPoint())
            if cell == getattr(self, "_last_mouse_cell", None):
                return  # 同一セル内の移動は報告しない (洪水防止)
            self._last_mouse_cell = cell
            code = pressed if pressed is not None else 3  # 3 = ボタンなし移動
            self._send_mouse(code, ev.position().toPoint(),
                             pressed=True, motion=True)
            return
        if ev.buttons() & Qt.LeftButton and self._sel_anchor is not None:
            self._sel_end = self._cell_at(ev.position().toPoint())
            self._dirty = True

    def mouseReleaseEvent(self, ev):
        pressed = getattr(self, "_mouse_pressed_code", None)
        if pressed is not None and ev.button() in self._QT_MOUSE_BTN:
            self._mouse_pressed_code = None
            if self._channel is not None and not self._closed \
                    and getattr(self.screen, "mouse_tracking", 0) != 0:
                self._send_mouse(pressed, ev.position().toPoint(), pressed=False)
            return
        if ev.button() == Qt.LeftButton and self._has_selection():
            self.copy_selection()  # PuTTY 流: 選択したら即コピー

    def contextMenuEvent(self, ev):
        # マウスレポート中は右クリックもアプリへ送っている (メニューは Shift 併用)
        if self._mouse_report_active(ev.modifiers()):
            return
        # 右クリック貼り付けが有効なら Shift 併用時だけメニュー表示
        if self._right_click_paste and not (ev.modifiers() & Qt.ShiftModifier):
            return
        menu = QMenu(self)
        act_copy = menu.addAction("コピー")
        act_copy.setEnabled(self._has_selection())
        act_paste = menu.addAction("貼り付け")
        menu.addSeparator()
        act_pw = menu.addAction("🔑 保存したパスワードを送信")
        act_clear_sel = menu.addAction("選択解除")
        if self._snippet_store is not None and self._snippet_store.snippets:
            menu.addSeparator()
            m_snippets = menu.addMenu("スニペット")
            for snippet in self._snippet_store.snippets:
                act = m_snippets.addAction(snippet.name)
                act.setToolTip(snippet.body)
                act.triggered.connect(partial(self._send_snippet, snippet))
        chosen = menu.exec(ev.globalPos())
        if chosen == act_copy:
            self.copy_selection()
        elif chosen == act_paste:
            self.paste_clipboard()
        elif chosen == act_pw:
            self.password_prompt.emit("manual")  # SessionTab が実送信
        elif chosen == act_clear_sel:
            self._sel_anchor = self._sel_end = None
            self._dirty = True

    def set_snippet_store(self, store):
        """右クリックメニューに表示する SnippetStore を設定する。"""
        self._snippet_store = store

    def _send_snippet(self, snippet):
        """スニペットを変数置換してターミナルへ送信する。"""
        values = SnippetVariablesDialog.ask(self, snippet.body)
        if values is None:
            return
        text = expand_snippet(snippet.body, values)
        if snippet.send_enter:
            text += "\n"
        self.send_text(text)

    def _has_selection(self) -> bool:
        return (
            self._sel_anchor is not None
            and self._sel_end is not None
            and self._sel_anchor != self._sel_end
        )

    def _sel_range(self):
        """選択範囲を線形順 (start, end) で返す。end は含む。"""
        a = self._sel_anchor
        b = self._sel_end
        if a is None or b is None:
            return None
        ai = a[0] * self._cols + a[1]
        bi = b[0] * self._cols + b[1]
        return (min(ai, bi), max(ai, bi))

    def copy_selection(self):
        rng = self._sel_range()
        if rng is None:
            return
        start, end = rng
        lines = []
        r0, r1 = start // self._cols, end // self._cols
        for row in range(r0, r1 + 1):
            c0 = start % self._cols if row == r0 else 0
            c1 = end % self._cols if row == r1 else self._cols - 1
            buf_row = self.screen.buffer[row]
            chars = []
            col = c0
            while col <= c1:
                ch = buf_row[col]
                if ch.data:
                    chars.append(ch.data)
                    w = wcwidth(ch.data)
                    col += w if w and w > 0 else 1
                else:
                    col += 1
            lines.append("".join(chars).rstrip())
        text = "\n".join(lines)
        if text:
            QGuiApplication.clipboard().setText(text)

    # ---- 描画 -------------------------------------------------------------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), self._c_bg)
        p.setFont(self._font)

        sel = self._sel_range() if self._has_selection() else None
        cw, chh = self._cw, self._chh
        buffer = self.screen.buffer

        for row in range(self._rows):
            line = buffer[row]
            url_ranges = self._parse_urls(row)
            url_cells = set()
            for sc, ec, _ in url_ranges:
                url_cells.update(range(sc, ec))
            y = row * chh
            col = 0
            while col < self._cols:
                ch = line[col]
                data = ch.data
                w = 1
                if data:
                    ww = wcwidth(data)
                    if ww and ww > 1:
                        w = ww
                fg = self._resolve(ch.fg, self._c_fg)
                bg = self._resolve(ch.bg, self._c_bg)
                if getattr(ch, "reverse", False):
                    fg, bg = bg, fg
                is_url = col in url_cells
                if is_url:
                    fg = self._c_link
                in_sel = sel is not None and sel[0] <= row * self._cols + col <= sel[1]
                if in_sel:
                    bg = self._c_sel
                cell = QRectF(col * cw, y, cw * w, chh)
                if bg != self._c_bg or in_sel:
                    p.fillRect(cell, bg)
                if data and data != " ":
                    p.setPen(fg)
                    p.setFont(self._font_bold if ch.bold else self._font)
                    p.drawText(cell, Qt.AlignLeft | Qt.AlignVCenter, data)
                    if getattr(ch, "underscore", False) or is_url:
                        p.drawLine(
                            int(cell.left()), int(cell.bottom() - 1),
                            int(cell.right()), int(cell.bottom() - 1),
                        )
                col += w

        # カーソル (最下部表示中のみ)
        cur = self.screen.cursor
        if not cur.hidden and not self._is_scrolled() and not self._closed:
            crect = QRectF(cur.x * cw, cur.y * chh, cw, chh)
            if self.hasFocus():
                p.fillRect(crect, self._c_cursor)
                ch = buffer[cur.y][cur.x]
                if ch.data and ch.data != " ":
                    p.setPen(self._c_bg)
                    p.setFont(self._font_bold if ch.bold else self._font)
                    p.drawText(crect, Qt.AlignLeft | Qt.AlignVCenter, ch.data)
            else:
                p.setPen(self._c_cursor)
                p.drawRect(crect.adjusted(0, 0, -1, -1))

        # IME 変換中テキストをカーソル位置に表示
        if self._preedit:
            p.setPen(QColor("#e5c07b"))
            p.setFont(self._font)
            p.drawText(
                QRectF(cur.x * cw, cur.y * chh, self.width() - cur.x * cw, chh),
                Qt.AlignLeft | Qt.AlignVCenter, self._preedit,
            )

        # 切断バナー
        if self._closed:
            p.fillRect(QRectF(0, 0, self.width(), chh + 8), QColor(120, 30, 30, 200))
            p.setPen(QColor("#ffffff"))
            p.drawText(
                QRectF(8, 4, self.width() - 16, chh),
                Qt.AlignLeft | Qt.AlignVCenter,
                "―― 切断されました。タブを閉じて再接続してください ――",
            )
        p.end()

    # ---- 後始末 --------------------------------------------------------------
    def detach(self):
        self._closed = True
        if self._session_log is not None:
            try:
                self._session_log.flush_visible(self.screen)
            except Exception:
                logger.debug("セッションログの終了 flush に失敗", exc_info=True)
            try:
                self._session_log.close()
            except Exception:
                logger.debug("セッションログの close に失敗", exc_info=True)
            self._session_log = None
        ch, self._channel = self._channel, None
        if ch is not None:
            try:
                ch.close()
            except Exception:
                logger.debug("ターミナルチャネルの close に失敗 (無視)", exc_info=True)
