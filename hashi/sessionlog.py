"""セッションログの自動保存 (PuTTY logging 相当)。

ターミナルに受信した出力をプレーンテキストでローカルファイルへ追記する。
入力（キー送信）は記録せず、受信のみを対象とする。
"""
from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path
from threading import Lock

from wcwidth import wcwidth

from .config import config_dir

logger = logging.getLogger(__name__)


def _row_text(row, columns: int) -> str:
    """pyte の 1 行 (CharLine) をプレーンテキストへ。"""
    chars = []
    col = 0
    while col < columns:
        ch = row[col]
        data = ch.data
        if data:
            chars.append(data)
            w = wcwidth(data)
            col += w if w and w > 0 else 1
        else:
            col += 1
    return "".join(chars).rstrip()


class SessionLog:
    """ターミナル受信出力の追記ログ。"""

    def __init__(self, profile_name: str, directory: str | Path | None = None,
                 enabled: bool = True):
        self._enabled = enabled
        self._file = None
        self._path = None
        self._lock = Lock()
        self._prev_lines: list[str] | None = None
        self._prev_cursor_y: int | None = None
        self._logged_top_len = 0
        if enabled:
            self._open(profile_name, directory)

    def _open(self, profile_name: str, directory: str | Path | None):
        try:
            d = Path(directory) if directory else config_dir() / "logs"
            d.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r'[\\/:*?"<>|]', "_", profile_name or "default")
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            self._path = d / f"hashi-{safe_name}-{ts}.log"
            self._file = open(self._path, "a", encoding="utf-8", errors="replace")
            logger.debug("セッションログ開始: %s", self._path)
        except Exception:
            logger.warning("セッションログファイルを開けません: %s",
                           directory, exc_info=True)
            self._enabled = False
            self._file = None

    def is_open(self) -> bool:
        return self._file is not None and not self._file.closed

    def path(self) -> Path | None:
        return self._path

    def write_screen(self, screen):
        """受信後のスクリーン状態を受け取り、新規行を追記する。"""
        if not self._enabled or self._file is None or self._file.closed:
            return
        with self._lock:
            try:
                self._do_write(screen)
            except Exception:
                logger.warning("セッションログ書き込みに失敗。ログを停止します",
                               exc_info=True)
                self._close()

    def _do_write(self, screen):
        columns = screen.columns
        lines = screen.lines

        # history.top にスクロールアウトした行が溜まっている場合は追記
        current_top_len = len(screen.history.top)
        if current_top_len > self._logged_top_len:
            for row in list(screen.history.top)[self._logged_top_len:]:
                text = _row_text(row, columns)
                if text:
                    self._file.write(text + "\n")
            self._logged_top_len = current_top_len

        current_lines = [
            _row_text(screen.buffer[y], columns) for y in range(lines)
        ]
        cursor_y = screen.cursor.y

        # pyte の dirty 集合は再利用しない（UI 側で使っていることもあるため
        # ここでは読み取り専用とする）。
        if self._prev_lines is None:
            # 初回は表示されている非空行をすべて追記する。
            # 後続の呼び出しでは差分のみ追記する。
            for text in current_lines:
                text = text.rstrip()
                if text:
                    self._file.write(text + "\n")
            self._prev_lines = current_lines
            self._prev_cursor_y = cursor_y
            self._file.flush()
            return

        # 画面が上にスクロールしたかを判定
        shift_up = self._shift_up(self._prev_lines, current_lines)
        if shift_up:
            n = len(current_lines)
            start = n - shift_up
            for y in range(start, n):
                if y == cursor_y:
                    continue
                text = current_lines[y].rstrip()
                if text:
                    self._file.write(text + "\n")

        elif cursor_y != self._prev_cursor_y:
            # スクロールなしでカーソルが移動 = 確定した行を追記
            if cursor_y > self._prev_cursor_y:
                for y in range(self._prev_cursor_y, cursor_y):
                    if 0 <= y < lines:
                        text = current_lines[y].rstrip()
                        if text:
                            self._file.write(text + "\n")
            else:
                # カーソルが上に移動（画面クリア等）。直前の行を追記。
                prev = self._prev_cursor_y
                if prev is not None and 0 <= prev < lines:
                    text = self._prev_lines[prev].rstrip()
                    if text:
                        self._file.write(text + "\n")

        self._prev_lines = current_lines
        self._prev_cursor_y = cursor_y
        self._file.flush()

    @staticmethod
    def _shift_up(prev: list[str], current: list[str]) -> int:
        """current が prev を上にスクロールして新しい行が下に入った数を返す。"""
        n = min(len(prev), len(current))
        for k in range(n, -1, -1):
            if current[:k] == prev[-k:]:
                return len(current) - k
        return 0

    def flush_visible(self, screen):
        """セッション終了時などに、まだ書いていない表示行を追記する。"""
        if not self._enabled or self._file is None or self._file.closed:
            return
        with self._lock:
            try:
                columns = screen.columns
                lines = screen.lines
                current_top_len = len(screen.history.top)
                if current_top_len > self._logged_top_len:
                    for row in list(screen.history.top)[self._logged_top_len:]:
                        text = _row_text(row, columns)
                        if text:
                            self._file.write(text + "\n")
                    self._logged_top_len = current_top_len

                current_lines = [
                    _row_text(screen.buffer[y], columns) for y in range(lines)
                ]
                if self._prev_lines is None:
                    for text in current_lines:
                        if text.rstrip():
                            self._file.write(text.rstrip() + "\n")
                else:
                    for i, text in enumerate(current_lines):
                        text = text.rstrip()
                        if text and (i >= len(self._prev_lines)
                                     or text != self._prev_lines[i].rstrip()):
                            self._file.write(text + "\n")
                self._file.flush()
            except Exception:
                logger.warning("セッションログの最終 flush に失敗", exc_info=True)

    def close(self):
        with self._lock:
            self._close()

    def _close(self):
        if self._file:
            try:
                self._file.close()
            except Exception:
                logger.debug("セッションログの close に失敗", exc_info=True)
            self._file = None
