"""UI 共通スタイル(Issue #87)のテスト。"""
import re

from hashi import style


def test_palette_applied_from_style_constants(qapp):
    """main の Fusion パレットが style.py の定数から作られている(#111)。

    main.apply_dark_theme が style 定数を参照するので、適用後のパレット色が
    定数と一致することを確認する(片方だけ変えると落ちる)。"""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication

    import main
    main.apply_dark_theme(QApplication.instance())
    pal = QApplication.instance().palette()
    assert pal.color(QPalette.Window) == QColor(style.BG)
    assert pal.color(QPalette.Base) == QColor(style.BG_BASE)
    assert pal.color(QPalette.WindowText) == QColor(style.FG)
    assert pal.color(QPalette.Highlight) == QColor(style.ACCENT)


def test_colors_are_hex():
    for name in ("BG", "BG_BASE", "BG_RAISED", "FG", "FG_MUTED",
                 "FG_DISABLED", "ACCENT", "ACCENT_HOVER", "BORDER",
                 "WARN", "ERROR", "OK"):
        assert re.fullmatch(r"#[0-9a-f]{6}", getattr(style, name), re.I), name


def test_chip_style_variants():
    base = style.chip_style()
    assert "transparent" in base and style.BORDER in base
    active = style.chip_style(active=True)
    assert style.ACCENT in active
    danger = style.chip_style(active=True, danger=True)
    assert style.DANGER_BG in danger


def test_warning_and_muted_labels(qapp):
    w = style.warning_label("危険な操作です")
    assert w.text().startswith("⚠")
    assert style.WARN in w.styleSheet()
    assert w.wordWrap()

    m = style.muted_label("補足です")
    assert style.FG_MUTED in m.styleSheet()
    assert style.FG_MUTED in style.muted_span("x")


def test_dialog_sizes_are_three_tiers():
    assert style.DIALOG_S < style.DIALOG_M < style.DIALOG_L
    assert style.SPACING == 8
