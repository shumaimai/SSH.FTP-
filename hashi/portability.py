"""接続情報の書き出し / 読み込み(Issue #42)。

エクスポート形式はバージョン番号付きの JSON 1 ファイル:

- profiles: 接続プロファイル(踏み台含む)
- known_hosts: ホスト鍵フィンガープリント。これを一緒に持ち出すことで、
  移行先でも「同じサーバーなのに鍵が違う」警告(TOFU)がそのまま機能する
- secrets(任意): パスワード / パスフレーズ / sudo。**平文では絶対に書かない**。
  パスフレーズから scrypt で導出した鍵の Fernet で暗号化した blob のみ

読み込み時の known_hosts は「無いものだけ追加」。既存の記録は上書きしない
(インポートで鍵変更警告を黙らせる事故を防ぐ)。
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import KnownHosts, Profile, ProfileStore
from .jsonio import save_json_atomic

logger = logging.getLogger(__name__)

FORMAT = "hashi-export"
VERSION = 1
_SECRET_KINDS = ("password", "passphrase", "sudo")


class PortabilityError(Exception):
    """書き出し / 読み込みの失敗(メッセージはそのまま表示できる日本語)。"""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _encrypt_secrets(secrets: dict, passphrase: str) -> dict:
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(
        json.dumps(secrets).encode("utf-8"))
    return {"kdf": "scrypt", "salt": base64.b64encode(salt).decode("ascii"),
            "token": token.decode("ascii")}


def _decrypt_secrets(blob: dict, passphrase: str) -> dict:
    from cryptography.fernet import Fernet, InvalidToken

    try:
        salt = base64.b64decode(blob["salt"])
        token = blob["token"].encode("ascii")
    except (KeyError, TypeError, ValueError) as e:
        raise PortabilityError("秘密情報ブロックの形式が壊れています。") from e
    try:
        raw = Fernet(_derive_key(passphrase, salt)).decrypt(token)
    except InvalidToken as e:
        raise PortabilityError("パスフレーズが違います(復号できません)。") from e
    return json.loads(raw.decode("utf-8"))


def export_bundle(path: str | Path, profiles: list[Profile],
                  known_hosts: KnownHosts, credentials=None,
                  passphrase: str | None = None) -> dict:
    """接続情報を 1 ファイルへ書き出す。returns {"profiles": n, "secrets": n}。

    passphrase と credentials の両方が渡されたときだけ秘密情報を(暗号化して)
    含める。どちらか欠けたら秘密情報は一切書かない。
    """
    data = {
        "format": FORMAT,
        "version": VERSION,
        "profiles": [asdict(p) for p in profiles],
        "known_hosts": dict(known_hosts._data),
    }
    secret_count = 0
    if passphrase and credentials is not None:
        secrets: dict[str, dict[str, str]] = {}
        for p in profiles:
            entry = {}
            for kind in _SECRET_KINDS:
                try:
                    v = credentials.get(p, kind)
                except Exception:
                    logger.warning("秘密情報の読み出しに失敗: %s", kind,
                                   exc_info=True)
                    v = None
                if v:
                    entry[kind] = v
            if entry:
                secrets[p.id_str()] = entry
                secret_count += len(entry)
        if secrets:
            data["secrets"] = _encrypt_secrets(secrets, passphrase)
    try:
        save_json_atomic(Path(path), data, ensure_ascii=False, indent=2)
    except OSError as e:
        raise PortabilityError(f"書き出せませんでした: {e}") from e
    return {"profiles": len(profiles), "secrets": secret_count}


@dataclass
class Bundle:
    """読み込んだエクスポートファイルの中身。"""

    profiles: list[Profile] = field(default_factory=list)
    known_hosts: dict = field(default_factory=dict)
    encrypted_secrets: dict | None = None   # 未復号の blob(無ければ None)
    secrets: dict | None = None             # 復号済み {id_str: {kind: secret}}

    @property
    def has_encrypted_secrets(self) -> bool:
        return self.encrypted_secrets is not None

    def decrypt_secrets(self, passphrase: str) -> None:
        if self.encrypted_secrets is None:
            return
        self.secrets = _decrypt_secrets(self.encrypted_secrets, passphrase)


def load_bundle(path: str | Path) -> Bundle:
    """エクスポートファイルを読み込んで検証する(秘密情報は未復号のまま)。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PortabilityError(f"読み込めませんでした: {e}") from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise PortabilityError("Hashi のエクスポートファイルではありません。")
    version = data.get("version")
    if not isinstance(version, int) or version > VERSION:
        raise PortabilityError(
            f"新しいバージョンのエクスポート形式です (v{version})。"
            "Hashi を更新してから読み込んでください。")
    profiles = []
    for d in data.get("profiles", []):
        if isinstance(d, dict):
            try:
                profiles.append(Profile.from_dict(d))
            except TypeError:
                logger.warning("壊れたプロファイルをスキップ: %r", d)
    kh = data.get("known_hosts")
    enc = data.get("secrets")
    return Bundle(
        profiles=profiles,
        known_hosts=kh if isinstance(kh, dict) else {},
        encrypted_secrets=enc if isinstance(enc, dict) else None,
    )


def merge_bundle(bundle: Bundle, store: ProfileStore, known_hosts: KnownHosts,
                 credentials=None, overwrite: bool = False) -> dict:
    """読み込んだ内容を既存データへ統合する。

    - プロファイル: id_str が同じものは overwrite=True なら置き換え、False ならスキップ
    - known_hosts: **無いものだけ追加**(既存の記録は絶対に上書きしない。
      インポートで「鍵が変わった」警告を黙らせる事故を防ぐ)
    - secrets: 復号済みなら CredentialStore へ保存(対応するプロファイルの分のみ)
    returns {"added": n, "updated": n, "skipped": n, "hosts_added": n, "secrets": n}
    """
    counts = {"added": 0, "updated": 0, "skipped": 0,
              "hosts_added": 0, "secrets": 0}
    existing = {p.id_str(): i for i, p in enumerate(store.profiles)}
    for p in bundle.profiles:
        idx = existing.get(p.id_str())
        if idx is None:
            store.profiles.append(p)
            existing[p.id_str()] = len(store.profiles) - 1
            counts["added"] += 1
        elif overwrite:
            store.profiles[idx] = p
            counts["updated"] += 1
        else:
            counts["skipped"] += 1
            continue
        if credentials is not None and bundle.secrets:
            for kind, secret in bundle.secrets.get(p.id_str(), {}).items():
                if kind in _SECRET_KINDS and isinstance(secret, str):
                    if credentials.set(p, kind, secret):
                        counts["secrets"] += 1
    store.save()

    for key, entry in bundle.known_hosts.items():
        if key not in known_hosts._data and isinstance(entry, dict):
            known_hosts._data[key] = {
                "key_type": entry.get("key_type"),
                "fingerprint": entry.get("fingerprint"),
            }
            counts["hosts_added"] += 1
    known_hosts.save()
    return counts
