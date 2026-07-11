"""SSH 鍵ペアの生成と公開鍵のサーバー登録。"""
from __future__ import annotations

import errno
import logging
import os
import posixpath
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from paramiko.message import Message

logger = logging.getLogger(__name__)

KEY_TYPES = ("ed25519", "ecdsa", "rsa")
ECDSA_BITS = (256, 384, 521)
RSA_BITS = (2048, 3072, 4096)


class KeygenError(Exception):
    """鍵の生成または登録に失敗した。"""


def generate_key(
    key_type: str,
    bits: int | None = None,
    passphrase: str | None = None,
    comment: str = "",
    path: str | os.PathLike[str] | None = None,
) -> tuple[paramiko.PKey, str]:
    """鍵ペアを生成し、指定された場合は秘密鍵を書き込む。"""
    normalized = key_type.lower()
    if normalized == "ed25519":
        private = ed25519.Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        message = Message()
        message.add_string(paramiko.Ed25519Key.name)
        message.add_string(public)
        key = paramiko.Ed25519Key(msg=message)
        key._hashi_private_key = private
    elif normalized == "ecdsa":
        curves = {256: ec.SECP256R1, 384: ec.SECP384R1, 521: ec.SECP521R1}
        try:
            key = paramiko.ECDSAKey.generate(curve=curves[int(bits)]())
        except (KeyError, TypeError, ValueError) as exc:
            raise KeygenError("ECDSA のビット数が不正です。") from exc
    elif normalized == "rsa":
        if bits not in RSA_BITS:
            raise KeygenError("RSA のビット数が不正です。")
        key = paramiko.RSAKey.generate(bits=int(bits))
    else:
        raise KeygenError(f"未対応の鍵種別です: {key_type}")

    public_line = public_key_line(key, comment)
    if path is not None:
        write_private_key(key, path, passphrase)
    return key, public_line


def public_key_line(key: paramiko.PKey, comment: str = "") -> str:
    """PKey を OpenSSH の公開鍵 1 行へ変換する。"""
    line = f"{key.get_name()} {key.get_base64()}"
    comment = comment.strip()
    return f"{line} {comment}" if comment else line


def write_private_key(
    key: paramiko.PKey,
    path: str | os.PathLike[str],
    passphrase: str | None = None,
) -> None:
    """秘密鍵を保存し、POSIX では所有者のみ読み書き可能にする。"""
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    private = getattr(key, "_hashi_private_key", None)
    if private is not None:
        encryption = (
            serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
            if passphrase
            else serialization.NoEncryption()
        )
        data = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            encryption,
        )
        destination.write_bytes(data)
    else:
        key.write_private_key_file(str(destination), password=passphrase or None)
    if os.name != "nt":
        os.chmod(destination, 0o600)


def register_public_key(session, public_line: str) -> bool:
    """接続中のセッションへ公開鍵を重複なく登録する。

    SFTP で ``~/.ssh`` と ``authorized_keys`` を操作するため、公開鍵の内容を
    シェルへ埋め込まない。戻り値は新規追加したかどうか。
    """
    if not public_line.strip():
        raise KeygenError("公開鍵が空です。")
    rc, home, err = session.exec_command('printf "%s" "$HOME"')
    home = home.strip()
    if rc != 0 or not home or not home.startswith("/"):
        detail = err.strip() or "ホームディレクトリを取得できませんでした。"
        raise KeygenError(detail)

    ssh_dir = posixpath.join(home, ".ssh")
    authorized_keys = posixpath.join(ssh_dir, "authorized_keys")
    sftp = session.open_sftp()
    try:
        try:
            sftp.stat(ssh_dir)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise KeygenError("~/.ssh の状態を確認できませんでした。") from exc
            sftp.mkdir(ssh_dir, mode=0o700)
        sftp.chmod(ssh_dir, 0o700)

        try:
            with sftp.open(authorized_keys, "rb") as remote_file:
                existing = remote_file.read().decode("utf-8", "replace")
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise KeygenError(
                    "authorized_keys を読み込めませんでした。"
                ) from exc
            existing = ""

        identity = " ".join(public_line.split()[:2])
        already_exists = any(
            " ".join(line.split()[:2]) == identity
            for line in existing.splitlines()
            if len(line.split()) >= 2 and not line.lstrip().startswith("#")
        )
        if not already_exists:
            separator = "" if not existing or existing.endswith("\n") else "\n"
            content = f"{existing}{separator}{public_line.rstrip()}\n"
            with sftp.open(authorized_keys, "wb") as remote_file:
                remote_file.write(content.encode("utf-8"))
        sftp.chmod(authorized_keys, 0o600)
        return not already_exists
    finally:
        try:
            sftp.close()
        except Exception:
            logger.debug("公開鍵登録後の SFTP 切断に失敗しました", exc_info=True)
