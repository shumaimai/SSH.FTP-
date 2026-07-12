"""Hashi — SSH / SFTP クライアント  エントリポイント

起動: python main.py

ログ: 既定は WARNING 以上を標準エラーへ。環境変数 HASHI_LOG_LEVEL で
      レベルを変えられる (例: HASHI_LOG_LEVEL=DEBUG)。詳細は setup_logging を参照。
"""
import logging
import os
import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from hashi.mainwindow import LauncherWindow


def setup_logging() -> None:
    """アプリ全体のロギングを初期化する。

    既定レベルは WARNING(通常運用でうるさくならない)。原因調査時は
    ``HASHI_LOG_LEVEL=DEBUG python main.py`` のように環境変数で上げられる。
    各モジュールは logging.getLogger(__name__) を使い、握り潰していた例外を
    ここへ流す。
    """
    level_name = os.environ.get("HASHI_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    bg = QColor("#22262e")
    base = QColor("#1b1f27")
    text = QColor("#dcdfe4")
    p.setColor(QPalette.Window, bg)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, bg)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, QColor("#2b303b"))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.ToolTipBase, QColor("#2b303b"))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Highlight, QColor("#3d59a1"))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.PlaceholderText, QColor("#6b7280"))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor("#6b7280"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#6b7280"))
    app.setPalette(p)


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("Hashi")
    apply_dark_theme(app)
    win = LauncherWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
