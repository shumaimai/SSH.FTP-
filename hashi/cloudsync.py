"""アカウント同期(Issue #44): クラウドに接続情報を E2E 暗号化して置く。

方針(専用サーバー不要・クライアント完結):

- 置くのは #42 のバンドル(profiles + known_hosts、秘密情報は #42 と同じく
  含めるならパスフレーズ暗号化済み)。それを **端末側でさらにマスターパスフレーズ
  由来の鍵(scrypt + Fernet)で丸ごと暗号化** してからアップロードする。クラウド側
  には暗号化済み blob しか渡らない(E2E)。
- 置き場所は差し替え可能な `SyncBackend`(get/put の 2 メソッド)。Google Drive の
  appDataFolder 実装(`GoogleDriveBackend`)を用意するが、任意の WebDAV / S3 でも
  同じインターフェースで載る。
- 競合は「最後に書いた方が勝ち」。ダウンロードして取り込む前に、手元の現状を
  ローカルへバックアップしてから統合する(上書き前に必ず退避)。

crypto と同期ロジックは GUI・ネットワーク非依存で、フェイク backend で完全に
テストできる。Google のクライアントライブラリは遅延 import(未インストールでも
このモジュールの読み込み自体は失敗しない)。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Protocol

from . import portability
from .config import KnownHosts, ProfileStore, config_dir

logger = logging.getLogger(__name__)

ENVELOPE_FORMAT = "hashi-cloudsync"
ENVELOPE_VERSION = 1
REMOTE_NAME = "hashi-sync.json.enc"     # appDataFolder 内のファイル名


class CloudSyncError(Exception):
    """クラウド同期の失敗(メッセージはそのまま表示できる日本語)。"""


# ---- E2E 暗号(マスターパスフレーズ → scrypt → Fernet) ----------------------

def _derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_blob(data: bytes, passphrase: str) -> bytes:
    """バイト列をマスターパスフレーズで暗号化し、封筒 JSON(bytes)にして返す。"""
    from cryptography.fernet import Fernet

    if not passphrase:
        raise CloudSyncError("マスターパスフレーズを空にはできません。")
    salt = os.urandom(16)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(data)
    envelope = {
        "format": ENVELOPE_FORMAT,
        "version": ENVELOPE_VERSION,
        "kdf": "scrypt",
        "salt": base64.b64encode(salt).decode("ascii"),
        "token": token.decode("ascii"),
        "updated_at": time.time(),
    }
    return json.dumps(envelope).encode("utf-8")


def decrypt_blob(envelope_bytes: bytes, passphrase: str) -> bytes:
    """封筒 JSON を復号して元のバイト列を返す。"""
    from cryptography.fernet import Fernet, InvalidToken

    try:
        env = json.loads(envelope_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CloudSyncError("同期データの形式が壊れています。") from e
    if not isinstance(env, dict) or env.get("format") != ENVELOPE_FORMAT:
        raise CloudSyncError("Hashi の同期データではありません。")
    if env.get("version", 0) > ENVELOPE_VERSION:
        raise CloudSyncError(
            "新しいバージョンの同期形式です。Hashi を更新してください。")
    try:
        salt = base64.b64decode(env["salt"])
        token = env["token"].encode("ascii")
    except (KeyError, TypeError, ValueError) as e:
        raise CloudSyncError("同期データの形式が壊れています。") from e
    try:
        return Fernet(_derive_key(passphrase, salt)).decrypt(token)
    except InvalidToken as e:
        raise CloudSyncError(
            "マスターパスフレーズが違います(復号できません)。") from e


# ---- backend インターフェース ------------------------------------------------

class SyncBackend(Protocol):
    """クラウド保存先の最小インターフェース。"""

    def get(self) -> bytes | None:
        """保存済みの blob を返す。無ければ None。"""
        ...

    def put(self, data: bytes) -> None:
        """blob を保存(上書き)する。"""
        ...


# ---- 同期ロジック(push / pull) --------------------------------------------

def push(backend: SyncBackend, profiles, known_hosts, master_passphrase: str,
         credentials=None, secrets_passphrase: str | None = None) -> dict:
    """現在の接続情報を暗号化してクラウドへアップロードする。

    secrets_passphrase を渡すとバンドルに(#42 と同じ方式で暗号化した)秘密情報も
    含める。master_passphrase は封筒全体の E2E 暗号に使う(別のパスフレーズでよい)。
    returns {"profiles": n, "secrets": n, "bytes": m}。
    """
    payload = portability.dumps_bundle(
        profiles, known_hosts,
        credentials if secrets_passphrase else None, secrets_passphrase)
    envelope = encrypt_blob(payload, master_passphrase)
    try:
        backend.put(envelope)
    except CloudSyncError:
        raise
    except Exception as e:  # noqa: BLE001 - backend 実装依存の失敗を日本語化
        raise CloudSyncError(f"アップロードに失敗しました: {e}") from e
    _, secret_count = portability.build_bundle_dict(
        profiles, known_hosts,
        credentials if secrets_passphrase else None, secrets_passphrase)
    return {"profiles": len(profiles), "secrets": secret_count,
            "bytes": len(envelope)}


def pull(backend: SyncBackend, master_passphrase: str):
    """クラウドから取得・復号して portability.Bundle を返す。無ければ None。"""
    try:
        envelope = backend.get()
    except CloudSyncError:
        raise
    except Exception as e:  # noqa: BLE001
        raise CloudSyncError(f"ダウンロードに失敗しました: {e}") from e
    if envelope is None:
        return None
    payload = decrypt_blob(envelope, master_passphrase)
    return portability.loads_bundle(payload)


def backup_local(store: ProfileStore, known_hosts: KnownHosts) -> Path:
    """取り込み前に手元の接続情報をローカルへ退避する(上書き前の保険)。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = config_dir() / f"sync-backup-{ts}.json"
    portability.export_bundle(dest, store.profiles, known_hosts)
    return dest


def pull_and_merge(backend: SyncBackend, master_passphrase: str,
                   store: ProfileStore, known_hosts: KnownHosts,
                   credentials=None, secrets_passphrase: str | None = None,
                   overwrite: bool = True) -> dict:
    """ダウンロード → 手元をバックアップ → 統合(既定は last-write-wins)。

    returns merge_bundle のカウント + {"backup": path, "empty": bool}。
    """
    bundle = pull(backend, master_passphrase)
    if bundle is None:
        return {"empty": True, "added": 0, "updated": 0, "skipped": 0,
                "hosts_added": 0, "secrets": 0, "backup": None}
    if bundle.has_encrypted_secrets and secrets_passphrase and credentials:
        try:
            bundle.decrypt_secrets(secrets_passphrase)
        except portability.PortabilityError as e:
            raise CloudSyncError(str(e)) from e
    backup = backup_local(store, known_hosts)
    counts = portability.merge_bundle(
        bundle, store, known_hosts, credentials, overwrite=overwrite)
    counts["backup"] = str(backup)
    counts["empty"] = False
    return counts


# ---- Google Drive backend(appDataFolder) -----------------------------------

# デスクトップアプリ用の OAuth クライアント。実運用では自分の GCP プロジェクトで
# 発行した client_id / client_secret を環境変数か設定で差し込む(同梱も可)。
_GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.appdata"]
_TOKEN_PATH = "google-oauth-token.json"


class GoogleDriveBackend:
    """Google Drive の appDataFolder に blob を 1 ファイルで置く backend。

    google-api-python-client / google-auth-oauthlib が必要(遅延 import)。
    未インストールなら分かりやすいエラーにする。認証トークンは config_dir に
    保存して次回以降は無言でリフレッシュする。
    """

    def __init__(self, client_config: dict | None = None,
                 token_path: Path | None = None):
        self.client_config = client_config or _default_client_config()
        self.token_path = token_path or (config_dir() / _TOKEN_PATH)
        self._service = None
        self._file_id: str | None = None

    def _creds(self):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as e:
            raise CloudSyncError(
                "Google 同期には追加ライブラリが必要です。\n"
                "pip install google-api-python-client google-auth-oauthlib") from e

        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self.token_path), _GOOGLE_SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not self.client_config:
                raise CloudSyncError(
                    "Google の OAuth クライアント設定がありません"
                    "(client_id / client_secret)。")
            flow = InstalledAppFlow.from_client_config(
                self.client_config, _GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)   # ループバックリダイレクト
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            logger.debug("トークンの chmod 0600 に失敗 (続行)", exc_info=True)
        return creds

    def _svc(self):
        if self._service is None:
            from googleapiclient.discovery import build
            self._service = build("drive", "v3", credentials=self._creds(),
                                  cache_discovery=False)
        return self._service

    def _find_file_id(self) -> str | None:
        if self._file_id:
            return self._file_id
        res = self._svc().files().list(
            spaces="appDataFolder",
            q=f"name = '{REMOTE_NAME}'",
            fields="files(id, name)").execute()
        files = res.get("files", [])
        if files:
            self._file_id = files[0]["id"]
        return self._file_id

    def get(self) -> bytes | None:
        fid = self._find_file_id()
        if not fid:
            return None
        return self._svc().files().get_media(fileId=fid).execute()

    def put(self, data: bytes) -> None:
        from googleapiclient.http import MediaInMemoryUpload
        media = MediaInMemoryUpload(data, mimetype="application/octet-stream")
        fid = self._find_file_id()
        if fid:
            self._svc().files().update(fileId=fid, media_body=media).execute()
        else:
            meta = {"name": REMOTE_NAME, "parents": ["appDataFolder"]}
            created = self._svc().files().create(
                body=meta, media_body=media, fields="id").execute()
            self._file_id = created["id"]


def _default_client_config() -> dict | None:
    """環境変数から OAuth クライアント設定を組む(無ければ None)。"""
    cid = os.environ.get("HASHI_GOOGLE_CLIENT_ID")
    secret = os.environ.get("HASHI_GOOGLE_CLIENT_SECRET")
    if not cid or not secret:
        return None
    return {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
