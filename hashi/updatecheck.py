"""起動時の新バージョン通知(Issue #101)。

GitHub Releases の最新タグをバックグラウンドで取得し、実行中のバージョンより
新しければランチャーへ通知する。失敗時は無音。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from PySide6.QtCore import QThread, Signal

from hashi import __version__ as _current_version

logger = logging.getLogger(__name__)

DEFAULT_RELEASES_URL = (
    "https://api.github.com/repos/shumaimai/Free-SSH_FTP/releases/latest"
)
_REQUEST_TIMEOUT = 5


def _parse_version(v: str) -> tuple[int, ...]:
    """v0.7.0 / 0.7.0-beta 等を (0, 7, 0) のように数値タプルへ変換する。"""
    v = v.strip().lstrip("vV")
    parts = v.split(".")
    out: list[int] = []
    for p in parts:
        m = re.match(r"(\d+)", p)
        if not m:
            raise ValueError(f"バージョン文字列が解析できません: {v!r}")
        out.append(int(m.group(1)))
    return tuple(out)


def _is_newer(current: str, latest: str) -> bool:
    """latest が current より新しいかを比較する。"""
    return _parse_version(latest) > _parse_version(current)


class UpdateCheckWorker(QThread):
    """GitHub Releases の最新タグを調べ、新しいバージョンがあれば通知する。"""

    new_version = Signal(str, str)  # tag_name, html_url

    def __init__(
        self,
        current_version: str = _current_version,
        releases_url: str = DEFAULT_RELEASES_URL,
        parent=None,
    ):
        super().__init__(parent)
        self.current_version = current_version
        self.releases_url = releases_url

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                self.releases_url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"Hashi/{self.current_version}",
                },
            )
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name", "")
            url = data.get("html_url", "")
            if not tag or not url:
                logger.debug(
                    "GitHub Releases API から必要なフィールドが返りませんでした。"
                )
                return
            latest = tag.lstrip("vV")
            if not _is_newer(self.current_version, latest):
                return
            self.new_version.emit(tag, url)
        except Exception:
            # 起動時のバックグラウンドチェックなので、失敗は静かに握り潰す
            logger.debug("新バージョン確認に失敗しました", exc_info=True)
