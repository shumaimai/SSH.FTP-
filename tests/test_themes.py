"""ターミナル配色テーマ(Issue #78)のテスト。"""
import re

from hashi import themes


def test_all_themes_have_complete_palettes():
    required_ansi = set(themes._ANSI_ORDER) | {"brown", "brightbrown"}
    hexpat = re.compile(r"#[0-9a-f]{6}", re.I)
    for name, t in themes.THEMES.items():
        for key in ("foreground", "background", "cursor", "selection"):
            assert hexpat.fullmatch(t[key]), f"{name}.{key}"
        assert set(t["ansi"]) == required_ansi, name
        for v in t["ansi"].values():
            assert hexpat.fullmatch(v), name
        # pyte の brown 別名は yellow と一致させる決まり
        assert t["ansi"]["brown"] == t["ansi"]["yellow"], name


def test_get_theme_fallback():
    assert themes.get_theme("Dracula")["background"] == "#282a36"
    assert themes.get_theme("存在しないテーマ") is themes.THEMES[themes.DEFAULT_THEME]
    assert themes.get_theme(None) is themes.THEMES[themes.DEFAULT_THEME]
    assert themes.DEFAULT_THEME in themes.theme_names()


def test_terminal_set_theme_changes_colors(qapp):
    from PySide6.QtGui import QColor

    from hashi.terminal import TerminalWidget

    t = TerminalWidget(theme="One Half Dark")
    assert t._c_bg == QColor("#1b1f27")
    # ANSI 名の解決がテーマに追従する
    assert t._resolve("red", t._c_fg) == QColor("#e06c75")

    t.set_theme("Solarized Light")
    assert t._c_bg == QColor("#fdf6e3")
    assert t._resolve("red", t._c_fg) == QColor("#dc322f")
    # 16 進 6 桁の直接指定 / 未知の名前はフォールバック
    assert t._resolve("ff0000", t._c_fg) == QColor("#ff0000")
    assert t._resolve("nosuchcolor", t._c_fg) == t._c_fg
    t.deleteLater()


def test_terminal_font_family_override(qapp):
    from hashi.terminal import TerminalWidget

    t = TerminalWidget(font_family="DejaVu Sans Mono")
    assert t._font.families()[0] == "DejaVu Sans Mono"
    t2 = TerminalWidget()
    assert t2._font.families()[0] == "Consolas"
    t.deleteLater()
    t2.deleteLater()
