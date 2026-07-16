"""ターミナル配色テーマ(Issue #78)。

各テーマは前景/背景/カーソル/選択色 + ANSI 16 色を持つ。ANSI 色のキーは
pyte の色名に合わせる(pyte は SGR 33 を "brown" と呼ぶため、yellow の
別名として brown / brightbrown も必ず持たせる)。
"""

DEFAULT_THEME = "One Half Dark"

_ANSI_ORDER = [
    "black", "red", "green", "yellow",
    "blue", "magenta", "cyan", "white",
    "brightblack", "brightred", "brightgreen", "brightyellow",
    "brightblue", "brightmagenta", "brightcyan", "brightwhite",
]


def _ansi(colors16: list[str]) -> dict[str, str]:
    """16 色リスト → pyte 色名 dict(brown 別名込み)。"""
    d = dict(zip(_ANSI_ORDER, colors16))
    d["brown"] = d["yellow"]
    d["brightbrown"] = d["brightyellow"]
    return d


THEMES: dict[str, dict] = {
    "One Half Dark": {
        "foreground": "#dcdfe4", "background": "#1b1f27",
        "cursor": "#dcdfe4", "selection": "#3e4b63",
        "ansi": _ansi([
            "#3b4048", "#e06c75", "#98c379", "#e5c07b",
            "#61afef", "#c678dd", "#56b6c2", "#dcdfe4",
            "#5c6370", "#e06c75", "#98c379", "#e5c07b",
            "#61afef", "#c678dd", "#56b6c2", "#ffffff",
        ]),
    },
    "Solarized Dark": {
        "foreground": "#839496", "background": "#002b36",
        "cursor": "#839496", "selection": "#073642",
        "ansi": _ansi([
            "#073642", "#dc322f", "#859900", "#b58900",
            "#268bd2", "#d33682", "#2aa198", "#eee8d5",
            "#586e75", "#cb4b16", "#859900", "#b58900",
            "#268bd2", "#6c71c4", "#2aa198", "#fdf6e3",
        ]),
    },
    "Solarized Light": {
        "foreground": "#657b83", "background": "#fdf6e3",
        "cursor": "#657b83", "selection": "#eee8d5",
        "ansi": _ansi([
            "#073642", "#dc322f", "#859900", "#b58900",
            "#268bd2", "#d33682", "#2aa198", "#eee8d5",
            "#586e75", "#cb4b16", "#859900", "#b58900",
            "#268bd2", "#6c71c4", "#2aa198", "#fdf6e3",
        ]),
    },
    "Monokai": {
        "foreground": "#f8f8f2", "background": "#272822",
        "cursor": "#f8f8f2", "selection": "#49483e",
        "ansi": _ansi([
            "#272822", "#f92672", "#a6e22e", "#e6db74",
            "#66d9ef", "#ae81ff", "#a1efe4", "#f8f8f2",
            "#75715e", "#f92672", "#a6e22e", "#e6db74",
            "#66d9ef", "#ae81ff", "#a1efe4", "#f9f8f5",
        ]),
    },
    "Dracula": {
        "foreground": "#f8f8f2", "background": "#282a36",
        "cursor": "#f8f8f2", "selection": "#44475a",
        "ansi": _ansi([
            "#21222c", "#ff5555", "#50fa7b", "#f1fa8c",
            "#bd93f9", "#ff79c6", "#8be9fd", "#f8f8f2",
            "#6272a4", "#ff6e6e", "#69ff94", "#ffffa5",
            "#d6acff", "#ff92df", "#a4ffff", "#ffffff",
        ]),
    },
    "Nord": {
        "foreground": "#d8dee9", "background": "#2e3440",
        "cursor": "#d8dee9", "selection": "#434c5e",
        "ansi": _ansi([
            "#3b4252", "#bf616a", "#a3be8c", "#ebcb8b",
            "#81a1c1", "#b48ead", "#88c0d0", "#e5e9f0",
            "#4c566a", "#bf616a", "#a3be8c", "#ebcb8b",
            "#81a1c1", "#b48ead", "#8fbcbb", "#eceff4",
        ]),
    },
}


def theme_names() -> list[str]:
    return list(THEMES)


def get_theme(name: str | None) -> dict:
    """テーマ定義を返す。未知の名前は既定(One Half Dark)にフォールバック。"""
    return THEMES.get(name or "", THEMES[DEFAULT_THEME])
