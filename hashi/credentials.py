"""認証情報 (パスワード / パスフレーズ / sudo パスワード) の保存。

保存先の優先順位:
  1. OS の資格情報ストア (Windows 資格情報マネージャ / macOS キーチェーン /
     Linux Secret Service) ― keyring 経由。Windows では DPAPI に守られる。
  2. それが使えない環境では config_dir/creds.dat に Fernet で暗号化して保存。
     鍵は config_dir/.credkey (0600) に置く。クラウド同期や覗き見に対する
     "保存時暗号化" であり、ローカルの本気の攻撃者に対する防御ではない。

kind は "password" / "passphrase" / "sudo" のいずれか。
プロファイル単位のキーは "user@host:port" で識別する。
"""
from __future__ import annotations

import json
import logging
import os

from .config import Profile, config_dir

logger = logging.getLogger(__name__)

SERVICE = "Hashi"
_KINDS = ("password", "passphrase", "sudo")


class _FernetFile:
    """cryptography.Fernet による暗号化ファイルバックエンド。"""

    def __init__(self):
        from cryptography.fernet import Fernet  # paramiko 依存で必ず存在
        self._Fernet = Fernet
        self.dir = config_dir()
        self.key_path = self.dir / ".credkey"
        self.data_path = self.dir / "creds.dat"
        self._fernet = self._Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes()
        key = self._Fernet.generate_key()
        self.key_path.write_bytes(key)
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            logger.debug("鍵ファイルの chmod 0600 に失敗 (続行): %s",
                         self.key_path, exc_info=True)
        return key

    def _load(self) -> dict:
        try:
            raw = self.data_path.read_bytes()
            return json.loads(self._fernet.decrypt(raw).decode("utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("認証情報ファイルを復号できません（空として扱います）: %s",
                           self.data_path, exc_info=True)
            return {}

    def _save(self, d: dict) -> None:
        blob = self._fernet.encrypt(json.dumps(d).encode("utf-8"))
        tmp = self.data_path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            logger.debug("creds.dat の chmod 0600 に失敗 (続行)", exc_info=True)
        tmp.replace(self.data_path)

    def get(self, key: str):
        return self._load().get(key)

    def set(self, key: str, value: str):
        d = self._load()
        d[key] = value
        self._save(d)

    def delete(self, key: str):
        d = self._load()
        if key in d:
            del d[key]
            self._save(d)


class CredentialStore:
    """認証情報の read/write。バックエンドは初回に自動選択。"""

    def __init__(self):
        self._keyring = None
        self._file = None
        self.backend_name = "none"
        self._init_backend()

    def _init_backend(self):
        # keyring に実バックエンドがあるか確認 (ダミーは弾く)
        try:
            import keyring
            from keyring.backends import fail as _fail
            kr = keyring.get_keyring()
            usable = not isinstance(kr, _fail.Keyring)
            # 一部環境の chainer は空。実際に書けるか軽く試す
            if usable:
                try:
                    keyring.set_password(SERVICE, "__probe__", "1")
                    keyring.delete_password(SERVICE, "__probe__")
                except Exception:
                    logger.debug("keyring 書き込みプローブ失敗→ファイルにフォールバック",
                                 exc_info=True)
                    usable = False
            if usable:
                self._keyring = keyring
                self.backend_name = type(kr).__name__
                return
        except Exception:
            logger.debug("keyring バックエンドの初期化に失敗→ファイルにフォールバック",
                         exc_info=True)
        # フォールバック: 暗号化ファイル
        try:
            self._file = _FernetFile()
            self.backend_name = "encrypted-file"
        except Exception:
            logger.warning("暗号化ファイルバックエンドも使えません。認証情報は保存されません",
                           exc_info=True)
            self.backend_name = "none"  # 保存不可 (メモリのみ)

    @property
    def available(self) -> bool:
        return self.backend_name != "none"

    def is_secure(self) -> bool:
        """OS 資格情報ストアなら True (ファイルフォールバックは False)。"""
        return self._keyring is not None

    def _key(self, profile: Profile, kind: str) -> str:
        return f"{profile.id_str()}:{kind}"

    def get(self, profile: Profile, kind: str) -> str | None:
        if kind not in _KINDS:
            raise ValueError(kind)
        key = self._key(profile, kind)
        try:
            if self._keyring is not None:
                return self._keyring.get_password(SERVICE, key)
            if self._file is not None:
                return self._file.get(key)
        except Exception:
            logger.warning("認証情報の取得に失敗: %s", kind, exc_info=True)
            return None
        return None

    def set(self, profile: Profile, kind: str, secret: str) -> bool:
        if kind not in _KINDS:
            raise ValueError(kind)
        key = self._key(profile, kind)
        try:
            if self._keyring is not None:
                self._keyring.set_password(SERVICE, key, secret)
                return True
            if self._file is not None:
                self._file.set(key, secret)
                return True
        except Exception:
            logger.warning("認証情報の保存に失敗: %s", kind, exc_info=True)
            return False
        return False

    def delete(self, profile: Profile, kind: str) -> None:
        key = self._key(profile, kind)
        try:
            if self._keyring is not None:
                self._keyring.delete_password(SERVICE, key)
            elif self._file is not None:
                self._file.delete(key)
        except Exception:
            logger.warning("認証情報の削除に失敗: %s", kind, exc_info=True)

    def clear_profile(self, profile: Profile) -> None:
        for kind in _KINDS:
            self.delete(profile, kind)
