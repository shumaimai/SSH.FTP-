"""UI 共通スタイル(Issue #87)。

アプリ全体の見た目を揃えるための単一ソース。**UI コードで色コードや
サイズを直書きせず、必ずここの定数/ヘルパーを使うこと。**
ルールの背景説明は docs/ui-style-guide.md を参照。

パレットは main.py の Fusion ダークテーマと一致させてある。テーマ側を
変えるときはここも同時に更新する。
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QLabel

# ---- カラーパレット(参考デザイン TransTerm、Issue #111。#RRGGBB のみ) ------
# main.py の Fusion パレットと一致させる(片方だけ変えると test_style が落ちる)。
BG = "#1e1f24"           # ウィンドウ背景
BG_BASE = "#191a1f"      # 入力欄・リスト背景(BG より一段暗い)
BG_RAISED = "#2a2b33"    # ボタン・チップ・ツールチップ背景
FG = "#e8e8ec"           # 基本テキスト
FG_MUTED = "#9a9ba6"     # 補足・注記
FG_DISABLED = "#6b6c78"  # 無効状態・プレースホルダ
ACCENT = "#4f8cff"       # 選択・強調・リンク・主ボタン
ACCENT_HOVER = "#7caaff"  # アクセントのホバー
BORDER = "#3a3b44"       # 枠線

# セマンティックカラー(意味が決まっている色。用途外に使わない)
WARN = "#d0a050"         # 警告(取り返しがつきにくい操作の注意書き)
ERROR = "#e0655f"        # エラー・危険・閉じる(参考の close 色)
DANGER_BG = "#7a3b3b"    # 危険スイッチ(権限無視等)ON 時の背景
OK = "#77c777"           # 成功・安全

# プロファイルの色マーカー(#81)。統一感のため自由入力ではなくこのプリセットのみ
PROFILE_COLORS: list[tuple[str, str]] = [
    ("なし", ""),
    ("レッド", "#e06c75"),
    ("オレンジ", "#d19a66"),
    ("イエロー", "#e5c07b"),
    ("グリーン", "#98c379"),
    ("シアン", "#56b6c2"),
    ("ブルー", "#61afef"),
    ("パープル", "#c678dd"),
]

# ---- 寸法(8px グリッド) ---------------------------------------------------
SPACING = 8              # 余白の基本単位。マージン/間隔は 8 の倍数を使う
DIALOG_S = 420           # 小: 入力 1〜3 個の単機能ダイアログ
DIALOG_M = 520           # 中: フォーム + 注意書き(標準)
DIALOG_L = 640           # 大: 一覧やプレビューを含むもの
TOAST_RADIUS = 6         # トースト等の角丸
CHIP_RADIUS = 6          # チップ型ボタンの角丸(参考デザイン #111)


def chip_style(active: bool = False, danger: bool = False) -> str:
    """チップ型ボタン(参考デザインのツールバー)の QSS を返す(#111)。

    active=True で押下状態(アクセント枠)、danger=True で ON 時に危険背景
    (権限無視スイッチ等)。
    """
    bg = DANGER_BG if (active and danger) else (BG_RAISED if active else "transparent")
    border = ACCENT if active and not danger else BORDER
    return (
        "QPushButton, QToolButton {"
        f" background:{bg}; color:{FG}; border:1px solid {border};"
        f" border-radius:{CHIP_RADIUS}px; padding:4px 10px; font-size:12px; }}"
        f"QPushButton:hover, QToolButton:hover {{ background:{BG_RAISED}; }}"
        f"QPushButton:disabled, QToolButton:disabled {{ color:{FG_DISABLED};"
        f" border-color:{BORDER}; background:transparent; }}"
    )

def info_chip(text: str, color: str = "") -> QLabel:
    """情報ステータスバー用の小さな丸みラベル(アイコン + 値)。"""
    lbl = QLabel(text)
    c = color or FG_MUTED
    lbl.setStyleSheet(
        f"color:{c}; padding:1px 4px; font-size:11px;")
    return lbl


# ---- アプリ全体のスタイルシート(Issue #113 / デザイン刷新) ------------------
# main.py で QApplication へ setStyleSheet する。色は必ずこの定数から取る。
# ターミナル本体は自前 QPainter 描画なので QSS の影響を受けない。
_APP_QSS = """
QToolTip {
    background: %(BG_RAISED)s; color: %(FG)s;
    border: 1px solid %(BORDER)s; border-radius: 6px; padding: 4px 8px;
}

QMenuBar { background: %(BG)s; border-bottom: 1px solid %(BORDER)s; padding: 2px 4px; }
QMenuBar::item { background: transparent; padding: 4px 10px; border-radius: 6px; }
QMenuBar::item:selected, QMenuBar::item:pressed { background: %(BG_RAISED)s; }
QMenu { background: %(BG_RAISED)s; border: 1px solid %(BORDER)s; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 20px; border-radius: 6px; }
QMenu::item:selected { background: %(ACCENT)s; color: #ffffff; }
QMenu::separator { height: 1px; background: %(BORDER)s; margin: 4px 8px; }

QTabWidget::pane { border: none; border-top: 1px solid %(BORDER)s; }
QTabBar::tab {
    background: transparent; color: %(FG_MUTED)s;
    padding: 7px 16px; margin-right: 2px;
    border: 1px solid transparent;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
}
QTabBar::tab:hover { background: %(BG_RAISED)s; color: %(FG)s; }
QTabBar::tab:selected {
    background: %(BG_BASE)s; color: %(FG)s;
    border-color: %(BORDER)s; border-bottom-color: %(BG_BASE)s;
}

QPushButton {
    background: %(BG_RAISED)s; color: %(FG)s;
    border: 1px solid %(BORDER)s; border-radius: %(R)spx;
    padding: 6px 14px;
}
QPushButton:hover { border-color: %(ACCENT)s; }
QPushButton:pressed { background: %(BG)s; }
QPushButton:disabled { color: %(FG_DISABLED)s; background: transparent; border-color: %(BORDER)s; }
QPushButton[primary="true"] {
    background: %(ACCENT)s; color: #ffffff; border: none; font-weight: bold;
}
QPushButton[primary="true"]:hover { background: %(ACCENT_HOVER)s; }
QPushButton[primary="true"]:disabled { background: %(BG_RAISED)s; color: %(FG_DISABLED)s; }

QToolButton {
    background: transparent; color: %(FG)s;
    border: 1px solid transparent; border-radius: %(R)spx; padding: 5px 9px;
}
QToolButton:hover { background: %(BG_RAISED)s; }
QToolButton:checked { background: %(BG_RAISED)s; border-color: %(ACCENT)s; }
QToolButton:disabled { color: %(FG_DISABLED)s; }

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {
    background: %(BG_BASE)s; color: %(FG)s;
    border: 1px solid %(BORDER)s; border-radius: %(R)spx;
    padding: 5px 8px; selection-background-color: %(ACCENT)s;
    selection-color: #ffffff;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QComboBox:focus { border-color: %(ACCENT)s; }
QComboBox::drop-down { border: none; width: 20px; }

QListWidget, QTreeWidget, QTreeView, QListView {
    background: %(BG_BASE)s; border: 1px solid %(BORDER)s; border-radius: 8px;
    outline: none;
}
QListWidget::item { padding: 6px 8px; border-radius: 6px; }
QListWidget::item:hover { background: %(BG_RAISED)s; }
QListWidget::item:selected { background: %(ACCENT)s; color: #ffffff; }
QTreeView::item:selected, QListView::item:selected,
QTreeWidget::item:selected { background: %(ACCENT)s; color: #ffffff; }
QHeaderView::section {
    background: %(BG)s; color: %(FG_MUTED)s;
    border: none; border-bottom: 1px solid %(BORDER)s; padding: 5px 8px;
}

QScrollBar:vertical { background: transparent; width: 11px; margin: 0; }
QScrollBar::handle:vertical { background: %(BORDER)s; border-radius: 5px; min-height: 32px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: %(FG_DISABLED)s; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 0; }
QScrollBar::handle:horizontal { background: %(BORDER)s; border-radius: 5px; min-width: 32px; margin: 2px; }
QScrollBar::handle:horizontal:hover { background: %(FG_DISABLED)s; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QStatusBar { background: %(BG)s; border-top: 1px solid %(BORDER)s; color: %(FG_MUTED)s; }
QStatusBar::item { border: none; }
QSplitter::handle { background: %(BORDER)s; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }

QProgressBar {
    background: %(BG_BASE)s; border: 1px solid %(BORDER)s; border-radius: 6px;
    text-align: center; color: %(FG)s; height: 14px;
}
QProgressBar::chunk { background: %(ACCENT)s; border-radius: 5px; }

QGroupBox {
    border: 1px solid %(BORDER)s; border-radius: 8px;
    margin-top: 10px; padding-top: 8px;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: %(FG_MUTED)s; }
QCheckBox, QRadioButton { spacing: 6px; }
"""


def app_stylesheet() -> str:
    """アプリ全体へ適用する QSS を返す(Issue #113)。色は必ず定数から。"""
    return _APP_QSS % {
        "BG": BG, "BG_BASE": BG_BASE, "BG_RAISED": BG_RAISED,
        "FG": FG, "FG_MUTED": FG_MUTED, "FG_DISABLED": FG_DISABLED,
        "ACCENT": ACCENT, "ACCENT_HOVER": ACCENT_HOVER, "BORDER": BORDER,
        "R": CHIP_RADIUS,
    }


# ---- ラベルヘルパー(注意書き・補足の見た目を統一) --------------------------


def warning_label(text: str) -> QLabel:
    """⚠ 付きの警告文ラベル。危険が伴う操作の説明に使う。"""
    if not text.startswith("⚠"):
        text = "⚠ " + text
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color:{WARN};")
    return lbl


def muted_label(text: str) -> QLabel:
    """補足・注記用の控えめなラベル。"""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color:{FG_MUTED};")
    return lbl


def muted_span(text: str) -> str:
    """リッチテキスト内で補足を控えめ色にする(<span> を返す)。"""
    return f"<span style='color:{FG_MUTED};'>{text}</span>"


def color_dot_icon(color: str, size: int = 12) -> QIcon:
    """色マーカー(●)アイコン。空文字なら控えめな輪郭だけの丸を返す。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    if color:
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
    else:
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QColor(BORDER))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pm)
