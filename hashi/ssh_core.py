"""SSH 接続コア (paramiko ラッパ)。

GUI 非依存。パスワード入力やホスト鍵確認は ui コールバック経由で行うため、
CLI (tools/doctor.py) からもそのまま使える。
"""
from __future__ import annotations

import base64
import hashlib
import socket

import paramiko

from .config import Profile, KnownHosts, AUTH_KEY, AUTH_PASSWORD, AUTH_AGENT


class ConnectCancelled(Exception):
    """ユーザーが接続手続きをキャンセルした。"""


class ConnectError(Exception):
    """接続/認証エラー(メッセージはそのまま表示できる日本語)。"""


def fingerprint_sha256(key: paramiko.PKey) -> str:
    """OpenSSH 互換の SHA256 フィンガープリント文字列。"""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def load_private_key(path: str, passphrase: str | None) -> paramiko.PKey:
    """秘密鍵ファイルを読み込む。形式は自動判別 (Ed25519 / ECDSA / RSA)。

    パスフレーズが必要なのに未指定なら paramiko.PasswordRequiredException を送出。
    """
    # paramiko >= 3.2 は自動判別ローダを持つ (引数名がバージョンで異なる)
    pw_bytes = passphrase.encode("utf-8") if passphrase else None
    if hasattr(paramiko.PKey, "from_path"):
        try:
            try:
                return paramiko.PKey.from_path(path, password=pw_bytes)
            except TypeError as e:
                if "unexpected keyword" in str(e):
                    # paramiko 3.x 系は引数名が passphrase
                    return paramiko.PKey.from_path(path, passphrase=pw_bytes)
                # cryptography は「パスフレーズ必要なのに未指定」を TypeError で返す
                raise paramiko.PasswordRequiredException(str(e)) from e
        except paramiko.PasswordRequiredException:
            raise
        except (paramiko.SSHException, ValueError):
            pass  # 形式違い/パスフレーズ誤りは下のフォールバックで再判定
    last_err: Exception | None = None
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key_file(path, password=passphrase)
        except paramiko.PasswordRequiredException:
            raise
        except Exception as e:  # noqa: BLE001 - 形式違いは順に試す
            last_err = e
    raise ConnectError(f"秘密鍵を読み込めませんでした: {last_err}")


class SshSession:
    """1 接続 = 1 Transport。シェルと SFTP は同じ Transport 上の別チャネル。"""

    def __init__(self, profile: Profile, known_hosts: KnownHosts | None = None):
        self.profile = profile
        self.known_hosts = known_hosts or KnownHosts()
        self.transport: paramiko.Transport | None = None

    # ---- 接続 -------------------------------------------------------------
    def connect(self, ui) -> None:
        """接続してホスト鍵検証と認証まで行う。

        ui に必要なメソッド:
          get_secret(prompt: str) -> str | None   (None ならキャンセル)
          confirm_hostkey(info: dict) -> bool     (True で信頼して続行)
        """
        p = self.profile
        if not p.host:
            raise ConnectError("ホスト名を入力してください。")
        if not p.username:
            raise ConnectError("ユーザー名を入力してください。")

        try:
            sock = socket.create_connection((p.host, p.port), timeout=10)
        except OSError as e:
            raise ConnectError(f"{p.host}:{p.port} に接続できません ({e})") from e

        t = paramiko.Transport(sock)
        t.set_keepalive(30)
        try:
            t.start_client(timeout=15)
        except paramiko.SSHException as e:
            t.close()
            raise ConnectError(f"SSH ネゴシエーションに失敗しました ({e})") from e

        try:
            self._verify_host_key(t, ui)
            self._authenticate(t, ui)
        except Exception:
            t.close()
            raise

        self.transport = t

    def _verify_host_key(self, t: paramiko.Transport, ui) -> None:
        p = self.profile
        server_key = t.get_remote_server_key()
        fp = fingerprint_sha256(server_key)
        key_type = server_key.get_name()
        status, old_fp = self.known_hosts.check(p.host, p.port, key_type, fp)
        if status == "match":
            return
        info = {
            "host": p.host,
            "port": p.port,
            "key_type": key_type,
            "fingerprint": fp,
            "status": status,          # "new" or "mismatch"
            "old_fingerprint": old_fp,
        }
        if not ui.confirm_hostkey(info):
            raise ConnectCancelled()
        self.known_hosts.remember(p.host, p.port, key_type, fp)

    def _authenticate(self, t: paramiko.Transport, ui) -> None:
        p = self.profile
        if p.auth_method == AUTH_KEY:
            self._auth_key(t, ui)
        elif p.auth_method == AUTH_AGENT:
            self._auth_agent(t)
        else:
            self._auth_password(t, ui)
        if not t.is_authenticated():
            raise ConnectError("認証に失敗しました。")

    def _auth_key(self, t: paramiko.Transport, ui) -> None:
        p = self.profile
        if not p.key_path:
            raise ConnectError("秘密鍵ファイルを指定してください。")
        passphrase: str | None = None
        pkey: paramiko.PKey | None = None
        for _ in range(3):  # パスフレーズ間違いは 3 回まで
            try:
                pkey = load_private_key(p.key_path, passphrase)
                break
            except paramiko.PasswordRequiredException:
                passphrase = ui.get_secret(
                    f"秘密鍵のパスフレーズを入力\n({p.key_path})"
                )
                if passphrase is None:
                    raise ConnectCancelled()
            except (paramiko.SSHException, ConnectError, ValueError):
                # パスフレーズ違いで復号失敗した場合など
                passphrase = ui.get_secret(
                    f"パスフレーズが違います。再入力してください\n({p.key_path})"
                )
                if passphrase is None:
                    raise ConnectCancelled()
        if pkey is None:
            raise ConnectError("秘密鍵を読み込めませんでした。")
        try:
            t.auth_publickey(p.username, pkey)
        except paramiko.AuthenticationException as e:
            raise ConnectError(
                "公開鍵認証に失敗しました。サーバー側の "
                "~/.ssh/authorized_keys に公開鍵が登録されているか確認してください。"
            ) from e

    def _auth_agent(self, t: paramiko.Transport) -> None:
        agent = paramiko.Agent()
        keys = agent.get_keys()
        if not keys:
            raise ConnectError(
                "SSH エージェントに鍵がありません。\n"
                "(Windows: OpenSSH Authentication Agent サービスを起動して ssh-add してください)"
            )
        last: Exception | None = None
        for k in keys:
            try:
                t.auth_publickey(self.profile.username, k)
                return
            except paramiko.AuthenticationException as e:
                last = e
        raise ConnectError(f"エージェント内のどの鍵でも認証できませんでした ({last})")

    def _auth_password(self, t: paramiko.Transport, ui) -> None:
        p = self.profile
        for _ in range(3):
            password = ui.get_secret(f"{p.username}@{p.host} のパスワードを入力")
            if password is None:
                raise ConnectCancelled()
            try:
                t.auth_password(p.username, password)
                return
            except paramiko.AuthenticationException:
                continue
        raise ConnectError("パスワード認証に失敗しました。")

    # ---- チャネル ----------------------------------------------------------
    def open_shell(self, cols: int = 80, rows: int = 24) -> paramiko.Channel:
        assert self.transport is not None
        ch = self.transport.open_session()
        ch.get_pty(term="xterm-256color", width=cols, height=rows)
        ch.invoke_shell()
        return ch

    def open_sftp(self) -> paramiko.SFTPClient:
        assert self.transport is not None
        sftp = paramiko.SFTPClient.from_transport(self.transport)
        if sftp is None:
            raise ConnectError("SFTP チャネルを開けませんでした。")
        return sftp

    # ---- コマンド実行 (権限無視スイッチ等が利用) --------------------------------
    def exec_command(self, command: str, timeout: float = 15.0):
        """1 コマンド実行。returns (exit_status, stdout, stderr)。"""
        assert self.transport is not None
        ch = self.transport.open_session()
        ch.settimeout(timeout)
        ch.exec_command(command)
        out, err = b"", b""
        try:
            while True:
                if ch.recv_ready():
                    out += ch.recv(65536)
                if ch.recv_stderr_ready():
                    err += ch.recv_stderr(65536)
                if ch.exit_status_ready() and not ch.recv_ready() \
                        and not ch.recv_stderr_ready():
                    break
        except Exception:
            pass
        rc = ch.recv_exit_status()
        # 残りを吸い出す
        while ch.recv_ready():
            out += ch.recv(65536)
        while ch.recv_stderr_ready():
            err += ch.recv_stderr(65536)
        ch.close()
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def run_sudo(self, command: str, password: str | None, timeout: float = 20.0):
        """sudo -S でコマンド実行 (パスワードを stdin から与える)。

        パスワード不要 (NOPASSWD 等) でも害はない。returns (rc, out, err)。
        """
        assert self.transport is not None
        ch = self.transport.open_session()
        ch.settimeout(timeout)
        # -k で過去の認証をリセットし、必ずプロンプトを stdin から処理させる
        ch.exec_command(f"sudo -S -p '' {command}")
        if password is not None:
            try:
                ch.sendall((password + "\n").encode("utf-8"))
            except Exception:
                pass
        out, err = b"", b""
        try:
            while not ch.exit_status_ready():
                if ch.recv_ready():
                    out += ch.recv(65536)
                if ch.recv_stderr_ready():
                    err += ch.recv_stderr(65536)
        except Exception:
            pass
        rc = ch.recv_exit_status()
        while ch.recv_ready():
            out += ch.recv(65536)
        while ch.recv_stderr_ready():
            err += ch.recv_stderr(65536)
        ch.close()
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def is_alive(self) -> bool:
        return bool(self.transport and self.transport.is_active())

    def close(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None
