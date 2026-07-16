"""UI 共通スタイル(Issue #87)。

アプリ全体の見た目を揃えるための単一ソース。**UI コードで色コードや
サイズを直書きせず、必ずここの定数/ヘルパーを使うこと。**
ルールの背景説明は docs/ui-style-guide.md を参照。

パレットは main.py の Fusion ダークテーマと一致させてある。テーマ側を
変えるときはここも同時に更新する。
"""
from PySide6.QtWidgets import QLabel

# ---- カラーパレット(Fusion ダークと同系。#RRGGBB のみ) --------------------
BG = "#22262e"           # ウィンドウ背景
BG_BASE = "#1b1f27"      # 入力欄・リスト背景
BG_RAISED = "#2b303b"    # ボタン・ツールチップ背景
FG = "#dcdfe4"           # 基本テキスト
FG_MUTED = "#8a919e"     # 補足・注記(旧 #888 系はこれに統一)
FG_DISABLED = "#6b7280"  # 無効状態・プレースホルダ
ACCENT = "#3d59a1"       # 選択・強調(パレットの Highlight と一致)
BORDER = "#444c56"       # 枠線

# セマンティックカラー(意味が決まっている色。用途外に使わない)
WARN = "#d0a050"         # 警告(取り返しがつきにくい操作の注意書き)
ERROR = "#e06c75"        # エラー・危険
DANGER_BG = "#7a3b3b"    # 危険スイッチ(権限無視等)ON 時の背景
OK = "#98c379"           # 成功・安全

# ---- 寸法(8px グリッド) ---------------------------------------------------
SPACING = 8              # 余白の基本単位。マージン/間隔は 8 の倍数を使う
DIALOG_S = 420           # 小: 入力 1〜3 個の単機能ダイアログ
DIALOG_M = 520           # 中: フォーム + 注意書き(標準)
DIALOG_L = 640           # 大: 一覧やプレビューを含むもの
TOAST_RADIUS = 6         # トースト等の角丸

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
