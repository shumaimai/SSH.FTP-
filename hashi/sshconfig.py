"""~/.ssh/config の読み込み(Host エイリアス解決)。

接続ダイアログのホスト欄に OpenSSH の Host エイリアスを書けるようにする。
対応キー: HostName / User / Port / IdentityFile。

ProxyJump は未対応。黙って無視すると「踏み台を経由したつもりが直接接続していた」
という事故になるため、検出したら例外で明示的に拒否する(Issue #3 のスコープ分割。
多段接続は別途対応)。
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import paramiko

from .config import AUTH_KEY, Profile


class UnsupportedOption(Exception):
    """~/.ssh/config に未対応の設定があった(黙殺すると危険なもの)。"""


def config_path() -> Path:
    """OpenSSH クライアント設定の標準パス(Windows も %USERPROFILE%\\.ssh)。"""
    return Path.home() / ".ssh" / "config"


def resolve_profile(profile: Profile, path: Path | None = None) -> Profile:
    """profile.host を Host エイリアスとして解決した Profile(コピー)を返す。

    - 設定ファイルが無い / 読めない場合は profile をそのまま返す。
    - Profile 側で明示されている値が常に優先:
        username 入力済み → User は使わない
        port が 22 以外   → Port は使わない(22 は未指定とみなす)
        key_path 指定済み → IdentityFile は使わない
    - ProxyJump が該当エントリにあれば UnsupportedOption を投げる。
    """
    p = path or config_path()
    try:
        with open(p, encoding="utf-8") as f:
            cfg = paramiko.SSHConfig()
            cfg.parse(f)
    except (OSError, UnicodeDecodeError):
        return profile

    entry = cfg.lookup(profile.host)

    if "proxyjump" in entry or "proxycommand" in entry:
        raise UnsupportedOption(
            f"~/.ssh/config の {profile.host} に ProxyJump/ProxyCommand が"
            "指定されていますが、Hashi は多段接続に未対応です。"
            "(黙って直接接続はしません)")

    host = entry.get("hostname", profile.host)
    port = profile.port
    if port == 22 and "port" in entry:
        try:
            port = int(entry["port"])
        except (TypeError, ValueError):
            pass
    username = profile.username or entry.get("user", "")
    key_path = profile.key_path
    if (profile.auth_method == AUTH_KEY and not key_path
            and entry.get("identityfile")):
        key_path = os.path.expanduser(entry["identityfile"][0])

    if (host, port, username, key_path) == (
            profile.host, profile.port, profile.username, profile.key_path):
        return profile
    return replace(profile, host=host, port=port,
                   username=username, key_path=key_path)
