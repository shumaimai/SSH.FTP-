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


def test_default_theme_harmonizes_with_app_palette():
    """既定テーマ Hashi はアプリのパレット(style)と調和させる(#113)。"""
    from hashi import style

    assert themes.DEFAULT_THEME == "Hashi"
    t = themes.get_theme("Hashi")
    # 前景はアプリの基本テキスト、カーソルはアクセント色に一致させる
    assert t["foreground"].lower() == style.FG.lower()
    assert t["cursor"].lower() == style.ACCENT.lower()
    # 背景はアプリの暗い面と近い(暗色であること)
    assert t["background"].startswith("#1")


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


def test_terminal_claims_keys_via_shortcut_override(qapp):
    """ウィンドウレベルのショートカットに打鍵を横取りさせない(#99)。
    Backspace がファイルブラウザの「上へ」に奪われる実機バグの再発防止。"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    from hashi.terminal import TerminalWidget

    t = TerminalWidget()
    ev = QKeyEvent(QEvent.ShortcutOverride, Qt.Key_Backspace, Qt.NoModifier)
    ev.ignore()
    assert t.event(ev) is True
    assert ev.isAccepted()
    t.deleteLater()


def test_apply_ui_settings_live(qapp):
    """設定保存後、開いているセッションへテーマ/フォントを即時反映(#99)。"""
    from types import SimpleNamespace

    from PySide6.QtGui import QColor

    from hashi.mainwindow import AppWindow, SessionPage
    from hashi.terminal import TerminalWidget

    term = TerminalWidget(theme="One Half Dark")

    class FakeSettings:
        def get(self, key):
            return {"terminal_theme": "Dracula",
                    "terminal_font_family": "DejaVu Sans Mono",
                    "terminal_font_size": 13}.get(key)

    fake_page = SimpleNamespace(session_tab=SimpleNamespace(terminal=term))
    SessionPage._pages.append(fake_page)
    try:
        fake_self = SimpleNamespace(settings=FakeSettings())
        AppWindow._apply_ui_settings_live(fake_self)
    finally:
        SessionPage._pages.remove(fake_page)

    assert term._c_bg == QColor("#282a36")     # Dracula の背景
    assert term._font.families()[0] == "DejaVu Sans Mono"
    assert term.font_size() == 13
    term.deleteLater()
