"""設定・接続プロファイル・known_hosts の永続化。

- プロファイル: %APPDATA%/Hashi/profiles.json (パスワードは絶対に保存しない)
- known_hosts: %APPDATA%/Hashi/known_hosts.json (ホスト鍵の SHA256 フィンガープリント)
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import __version__
from .jsonio import load_json, save_json_atomic

logger = logging.getLogger(__name__)

APP_NAME = "Hashi"
APP_VERSION = __version__   # バージョンの単一ソース (hashi/__init__.py)

AUTH_KEY = "key"
AUTH_PASSWORD = "password"
AUTH_AGENT = "agent"


def config_dir() -> Path:
    """OS ごとの設定ディレクトリを返す(なければ作成)。"""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Profile:
    """接続プロファイル。パスワード/パスフレーズ/sudo は keyring 等に別途保存。"""
    name: str = ""
    host: str = ""
    port: int = 22
    username: str = ""
    auth_method: str = AUTH_KEY   # key / password / agent
    key_path: str = ""            # auth_method == key のときの秘密鍵パス
    initial_path: str = ""        # 接続直後に開くリモートパス(空ならホーム)
    proxy_jump: str = ""          # 踏み台 (OpenSSH ProxyJump 書式。カンマ区切りで多段)
    save_secrets: bool = True     # パスワード/パスフレーズを保存するか
    sudo_same_as_password: bool = True  # sudo パスワード = ログインパスワード

    def label(self) -> str:
        return self.name or f"{self.username}@{self.host}"

    def id_str(self) -> str:
        return f"{self.username}@{self.host}:{self.port}"

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


class ProfileStore:
    """profiles.json の読み書き。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "profiles.json")
        self.profiles: list[Profile] = []
        self.load()

    def load(self) -> None:
        self.profiles = []
        try:
            data = load_json(
                self.path,
                list,
                logger=logger,
                warning="profiles.json を読み込めません(無視して続行): %s",
            )
            for d in data:
                self.profiles.append(Profile.from_dict(d))
        except Exception:
            # 壊れたファイルは無視(上書き保存で復旧)するが、警告は残す
            logger.warning("profiles.json を読み込めません(無視して続行): %s",
                           self.path, exc_info=True)
            self.profiles = []

    def save(self) -> None:
        save_json_atomic(
            self.path,
            [asdict(p) for p in self.profiles],
            ensure_ascii=False,
            indent=2,
        )

    def add(self, p: Profile) -> None:
        self.profiles.append(p)
        self.save()

    def update(self, index: int, p: Profile) -> None:
        self.profiles[index] = p
        self.save()

    def remove(self, index: int) -> None:
        del self.profiles[index]
        self.save()


class Settings:
    """アプリ全体の設定 (settings.json)。"""

    DEFAULTS = {
        "sudo_autofill": True,          # sudo プロンプト検知時に自動でパスワード送信
        "permission_override": False,   # SFTP 権限無視スイッチの既定
        "right_click_paste": True,      # 右クリックで貼り付け (PuTTY 流)
        "terminal_font_size": 11,
        "editor_font_size": 12,
        "editor_tab_width": 4,
        "open_text_in_editor": True,    # テキストは内蔵エディタで開く
    }

    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "settings.json")
        self._data = dict(self.DEFAULTS)
        self.load()

    def load(self):
        d = load_json(
            self.path,
            dict,
            logger=logger,
            warning="settings.json を読み込めません(既定値で続行): %s",
        )
        for k in self.DEFAULTS:
            if k in d:
                self._data[k] = d[k]

    def save(self):
        save_json_atomic(self.path, self._data, indent=2)

    def get(self, key: str):
        return self._data.get(key, self.DEFAULTS.get(key))

    def set(self, key: str, value):
        self._data[key] = value
        self.save()


class KnownHosts:
    """ホスト鍵の記録 (TOFU: Trust On First Use)。

    形式: {"host:port": {"key_type": "...", "fingerprint": "SHA256:..."}}
    """

    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "known_hosts.json")
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        self._data = load_json(
            self.path,
            dict,
            logger=logger,
            warning="known_hosts.json を読み込めません(空で続行): %s",
        )

    def save(self) -> None:
        save_json_atomic(self.path, self._data, indent=2)

    @staticmethod
    def _key(host: str, port: int) -> str:
        return f"{host}:{port}"

    def check(self, host: str, port: int, key_type: str, fingerprint: str):
        """returns (status, old_fingerprint)
        status: "new" (初回) / "match" (一致) / "mismatch" (鍵が変わった!)
        """
        entry = self._data.get(self._key(host, port))
        if entry is None:
            return "new", None
        if entry.get("fingerprint") == fingerprint and entry.get("key_type") == key_type:
            return "match", None
        return "mismatch", entry.get("fingerprint")

    def remember(self, host: str, port: int, key_type: str, fingerprint: str) -> None:
        self._data[self._key(host, port)] = {
            "key_type": key_type,
            "fingerprint": fingerprint,
        }
        self.save()
