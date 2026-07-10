"""内蔵コードエディタ。

リモートファイルを一時 DL → このエディタで編集 → Ctrl+S でサーバーへ書き戻し。
メモ帳や外部アプリを使わず、編集内容がそのまま SFTP アップロードされる。
権限が足りなければ(権限無視スイッチが ON なら)自動で権限を付けて保存する。

機能: 行番号、現在行ハイライト、拡張子ベースの簡易シンタックスハイライト、
     検索 (Ctrl+F / F3)、タブ幅設定、未保存の警告。
"""
from __future__ import annotations

import os
import re

from PySide6.QtCore import Qt, QRect, QSize, Signal
from PySide6.QtGui import (
    QColor, QFont, QPainter, QSyntaxHighlighter, QTextCharFormat,
    QTextFormat, QTextCursor, QKeySequence, QShortcut, QPalette,
)
from PySide6.QtWidgets import (
    QPlainTextEdit, QWidget, QTextEdit, QMainWindow, QLineEdit, QLabel,
    QHBoxLayout, QToolBar, QMessageBox, QStatusBar,
)

# ---- シンタックスハイライト規則 -------------------------------------------------
# 色 (One Half Dark 系)
C_KEYWORD = "#c678dd"
C_STRING = "#98c379"
C_COMMENT = "#7f848e"
C_NUMBER = "#d19a66"
C_FUNC = "#61afef"
C_DECORATOR = "#e5c07b"

_PY_KW = (
    "def class return if elif else for while break continue import from as pass "
    "with try except finally raise lambda yield global nonlocal assert del in is "
    "not and or None True False async await self match case"
).split()
_C_KW = (
    "int char float double void long short unsigned signed struct union enum "
    "return if else for while do break continue switch case default goto sizeof "
    "typedef const static extern volatile register auto include define ifdef "
    "ifndef endif pragma class public private protected virtual namespace using "
    "template new delete this true false nullptr bool"
).split()
_JS_KW = (
    "function return if else for while break continue var let const new class "
    "extends import export from default async await try catch finally throw "
    "typeof instanceof this null undefined true false switch case do yield of in "
    "delete void super static get set"
).split()
_SH_KW = (
    "if then else elif fi for while do done case esac function return in select "
    "until break continue local export readonly declare source echo cd exit set "
    "unset trap eval exec test"
).split()


def _lang_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    name = os.path.basename(path).lower()
    if ext in ("py", "pyw"):
        return "python"
    if ext in ("c", "h", "cpp", "cc", "hpp", "cxx", "java", "cs", "go", "rs"):
        return "c"
    if ext in ("js", "jsx", "ts", "tsx", "json"):
        return "js"
    if ext in ("sh", "bash", "zsh") or name in ("bashrc", ".bashrc", "profile"):
        return "shell"
    if ext in ("yml", "yaml", "conf", "cfg", "ini", "toml"):
        return "conf"
    return "plain"


class Highlighter(QSyntaxHighlighter):
    """拡張子に応じた軽量ハイライタ(依存ライブラリなし)。"""

    def __init__(self, document, lang: str):
        super().__init__(document)
        self.rules: list[tuple[re.Pattern, QTextCharFormat]] = []
        self.lang = lang
        self._build()

    @staticmethod
    def _fmt(color: str, bold=False, italic=False) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        if bold:
            f.setFontWeight(QFont.Bold)
        if italic:
            f.setFontItalic(True)
        return f

    def _build(self):
        lang = self.lang
        kw = {"python": _PY_KW, "c": _C_KW, "js": _JS_KW, "shell": _SH_KW}.get(lang)
        if kw:
            kw_fmt = self._fmt(C_KEYWORD, bold=True)
            self.rules.append(
                (re.compile(r"\b(" + "|".join(kw) + r")\b"), kw_fmt))
        # 関数呼び出し / 定義名
        if lang in ("python", "c", "js"):
            self.rules.append(
                (re.compile(r"\b([A-Za-z_]\w*)\s*(?=\()"), self._fmt(C_FUNC)))
        # 数値
        self.rules.append(
            (re.compile(r"\b\d+\.?\d*([eE][+-]?\d+)?\b"), self._fmt(C_NUMBER)))
        # 文字列 (シングル/ダブル)
        str_fmt = self._fmt(C_STRING)
        self.rules.append((re.compile(r'"[^"\\]*(\\.[^"\\]*)*"'), str_fmt))
        self.rules.append((re.compile(r"'[^'\\]*(\\.[^'\\]*)*'"), str_fmt))
        # デコレータ / プリプロセッサ
        if lang == "python":
            self.rules.append(
                (re.compile(r"^\s*@\w+"), self._fmt(C_DECORATOR)))
        # コメント (行コメントのみ簡易対応; 末尾で上書き)
        cmt_fmt = self._fmt(C_COMMENT, italic=True)
        if lang in ("python", "shell", "conf"):
            self._line_comment = (re.compile(r"#.*$"), cmt_fmt)
        elif lang in ("c", "js"):
            self._line_comment = (re.compile(r"//.*$"), cmt_fmt)
        else:
            self._line_comment = None
        self._cmt_fmt = cmt_fmt
        # C/JS ブロックコメント用
        self._block = lang in ("c", "js")

    def highlightBlock(self, text: str):
        for pattern, fmt in self.rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)
        # 行コメントは最後に上書き(文字列内 # を避けるため簡易に末尾優先)
        if self._line_comment is not None:
            pat, fmt = self._line_comment
            for m in pat.finditer(text):
                # 直前がクォート内でないかの厳密判定は省略(実用上十分)
                self.setFormat(m.start(), len(text) - m.start(), fmt)
        # C/JS ブロックコメント /* */
        if self._block:
            self._apply_block_comment(text)

    def _apply_block_comment(self, text: str):
        start_expr, end_expr = "/*", "*/"
        self.setCurrentBlockState(0)
        start = 0
        if self.previousBlockState() != 1:
            start = text.find(start_expr)
        while start >= 0:
            end = text.find(end_expr, start)
            if end == -1:
                self.setCurrentBlockState(1)
                length = len(text) - start
            else:
                length = end - start + len(end_expr)
            self.setFormat(start, length, self._cmt_fmt)
            start = text.find(start_expr, start + length)


# ---- 行番号エリア ---------------------------------------------------------------
class _LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, ev):
        self.editor.paint_line_numbers(ev)


class CodeEdit(QPlainTextEdit):
    """行番号 + 現在行ハイライト付きのエディタ本体。"""

    def __init__(self, parent=None, font_size=12, tab_width=4):
        super().__init__(parent)
        f = QFont()
        f.setFamilies(["Consolas", "Cascadia Mono", "MS Gothic", "Monospace"])
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(font_size)
        self.setFont(f)
        self.setTabStopDistance(
            self.fontMetrics().horizontalAdvance(" ") * tab_width)
        self._tab_width = tab_width
        self.setLineWrapMode(QPlainTextEdit.NoWrap)

        self._lna = _LineNumberArea(self)
        self.blockCountChanged.connect(self._update_lna_width)
        self.updateRequest.connect(self._update_lna)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_lna_width()
        self._highlight_current_line()

        pal = self.palette()
        pal.setColor(QPalette.Base, QColor("#1b1f27"))
        pal.setColor(QPalette.Text, QColor("#dcdfe4"))
        self.setPalette(pal)

    def line_number_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_lna_width(self):
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_lna(self, rect, dy):
        if dy:
            self._lna.scroll(0, dy)
        else:
            self._lna.update(0, rect.y(), self._lna.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_lna_width()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        cr = self.contentsRect()
        self._lna.setGeometry(QRect(cr.left(), cr.top(),
                                    self.line_number_width(), cr.height()))

    def _highlight_current_line(self):
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor("#242a35"))
        sel.format.setProperty(QTextFormat.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])

    def paint_line_numbers(self, ev):
        painter = QPainter(self._lna)
        painter.fillRect(ev.rect(), QColor("#181c23"))
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(
            self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        cur_line = self.textCursor().blockNumber()
        while block.isValid() and top <= ev.rect().bottom():
            if block.isVisible() and bottom >= ev.rect().top():
                painter.setPen(QColor("#61afef") if num == cur_line
                               else QColor("#4b5263"))
                painter.drawText(
                    0, int(top), self._lna.width() - 6,
                    self.fontMetrics().height(),
                    Qt.AlignRight, str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            num += 1


class EditorWindow(QMainWindow):
    """1 ファイル分の編集ウィンドウ。保存はコールバック経由でアップロード。"""

    closed = Signal(object)  # self

    def __init__(self, remote_path: str, local_path: str,
                 save_callback, settings, parent=None):
        super().__init__(parent)
        self.remote_path = remote_path
        self.local_path = local_path
        self._save_cb = save_callback
        self._saving = False

        self.editor = CodeEdit(
            font_size=settings.get("editor_font_size"),
            tab_width=settings.get("editor_tab_width"),
        )
        self.setCentralWidget(self.editor)
        self.resize(900, 640)

        # 読み込み
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                text = f.read()
            self._encoding = "utf-8"
        except UnicodeDecodeError:
            with open(local_path, "r", encoding="latin-1") as f:
                text = f.read()
            self._encoding = "latin-1"
        self.editor.setPlainText(text)
        self.editor.document().setModified(False)

        self._hl = Highlighter(self.editor.document(),
                               _lang_for(remote_path))

        self._build_toolbar()
        self.setStatusBar(QStatusBar())
        self.editor.document().modificationChanged.connect(self._update_title)
        self.editor.cursorPositionChanged.connect(self._update_cursor_status)
        self._update_title()

        QShortcut(QKeySequence.Save, self, self.save)
        QShortcut(QKeySequence.Find, self, self._focus_find)
        QShortcut(QKeySequence.FindNext, self, lambda: self._find(True))
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._hide_find)

    # ---- ツールバー / 検索 --------------------------------------------------
    def _build_toolbar(self):
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addAction("保存 (Ctrl+S)", self.save)
        tb.addSeparator()
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("検索 (Ctrl+F)…")
        self.find_edit.setMaximumWidth(240)
        self.find_edit.returnPressed.connect(lambda: self._find(True))
        tb.addWidget(self.find_edit)
        tb.addAction("次へ", lambda: self._find(True))
        tb.addAction("前へ", lambda: self._find(False))

    def _focus_find(self):
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def _hide_find(self):
        self.editor.setFocus()

    def _find(self, forward: bool):
        text = self.find_edit.text()
        if not text:
            return
        flags = QTextCursor.MoveAnchor
        found = self.editor.find(
            text, QPlainTextEdit.FindFlag(0) if forward
            else QPlainTextEdit.FindFlag.FindBackward)
        if not found:
            # 端まで来たら先頭/末尾へ回り込み
            cur = self.editor.textCursor()
            cur.movePosition(QTextCursor.Start if forward else QTextCursor.End)
            self.editor.setTextCursor(cur)
            self.editor.find(
                text, QPlainTextEdit.FindFlag(0) if forward
                else QPlainTextEdit.FindFlag.FindBackward)

    # ---- 保存 ---------------------------------------------------------------
    def save(self):
        if self._saving:
            return
        try:
            with open(self.local_path, "w", encoding=self._encoding) as f:
                f.write(self.editor.toPlainText())
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存", f"一時ファイルの書き込みに失敗:\n{e}")
            return
        self._saving = True
        self.statusBar().showMessage("サーバーへ保存中…")
        # save_callback(remote, local, done_callback)
        self._save_cb(self.remote_path, self.local_path, self._on_saved)

    def _on_saved(self, ok: bool, message: str):
        self._saving = False
        if ok:
            self.editor.document().setModified(False)
            self.statusBar().showMessage(f"保存しました: {self.remote_path}", 4000)
        else:
            self.statusBar().showMessage("保存に失敗しました", 4000)
            QMessageBox.warning(self, "保存エラー", message)
        self._update_title()

    # ---- タイトル / ステータス ------------------------------------------------
    def _update_title(self):
        dirty = "●" if self.editor.document().isModified() else ""
        self.setWindowTitle(
            f"{dirty}{os.path.basename(self.remote_path)} — {self.remote_path} [Hashi Editor]")

    def _update_cursor_status(self):
        c = self.editor.textCursor()
        self.statusBar().showMessage(
            f"行 {c.blockNumber() + 1}, 列 {c.columnNumber() + 1}", 0)

    def closeEvent(self, ev):
        if self.editor.document().isModified():
            r = QMessageBox.question(
                self, "未保存の変更",
                f"{os.path.basename(self.remote_path)} は未保存です。保存しますか?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            )
            if r == QMessageBox.Save:
                self.save()
                ev.ignore()  # 保存完了を待つため一旦キャンセル
                return
            if r == QMessageBox.Cancel:
                ev.ignore()
                return
        self.closed.emit(self)
        ev.accept()
