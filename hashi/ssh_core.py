"""SSH 接続コア (paramiko ラッパ)。

GUI 非依存。パスワード入力やホスト鍵確認は ui コールバック経由で行うため、
CLI (tools/doctor.py) からもそのまま使える。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import socket
from dataclasses import replace

import paramiko

from . import sshconfig
from .config import AUTH_AGENT, AUTH_KEY, KnownHosts, Profile

logger = logging.getLogger(__name__)

# ProxyJump の多段数上限(設定ミスによる無限チェーン/異常な深さを防ぐ)
MAX_JUMP_HOPS = 8


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


def parse_jump_specs(spec: str) -> list[tuple[str, str, int | None]]:
    """OpenSSH の ProxyJump 書式をパースして (user, host, port) のリストを返す。

    書式: ``[user@]host[:port]`` をカンマ区切りで多段。IPv6 は ``[::1]:22`` の
    ブラケット表記に対応(ブラケット無し・コロン複数は裸の IPv6 とみなす)。
    port は未指定なら None。不正な書式は ConnectError。
    """
    result: list[tuple[str, str, int | None]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        user = ""
        if "@" in part:
            user, part = part.rsplit("@", 1)
        port_s: str | None = None
        if part.startswith("["):
            end = part.find("]")
            if end < 0:
                raise ConnectError(f"ProxyJump の書式が不正です([ が閉じていません): {part}")
            host = part[1:end]
            rest = part[end + 1:]
            if rest.startswith(":"):
                port_s = rest[1:]
            elif rest:
                raise ConnectError(f"ProxyJump の書式が不正です: {part}")
        elif part.count(":") == 1:
            host, port_s = part.split(":")
        else:
            host = part  # コロン 0 個 = ホスト名 / 2 個以上 = 裸の IPv6
        port: int | None = None
        if port_s is not None:
            try:
                port = int(port_s)
            except ValueError:
                port = -1
            if not 1 <= port <= 65535:
                raise ConnectError(f"ProxyJump のポートが不正です: {part}")
        if not host:
            raise ConnectError(f"ProxyJump のホストが空です: {spec}")
        result.append((user, host, port))
    return result


def resolve_jump_chain(profile: Profile) -> list[Profile]:
    """profile.proxy_jump から踏み台の Profile リストを作る(接続順)。

    各踏み台も ~/.ssh/config のエイリアスとして解決する(IdentityFile 等を拾う)。
    踏み台自体にさらに ProxyJump が付いている入れ子は未対応で、黙って直結する
    事故を避けるため明示的にエラーにする(トップの ProxyJump に平坦化してもらう)。
    ユーザー名未指定の踏み台は接続先のユーザー名を流用する。
    """
    spec = (profile.proxy_jump or "").strip()
    if not spec or spec.lower() == "none":
        return []
    hops: list[Profile] = []
    for user, host, port in parse_jump_specs(spec):
        hop = Profile(host=host, port=port or 22, username=user,
                      auth_method=AUTH_KEY, save_secrets=False)
        try:
            hop = sshconfig.resolve_profile(hop)
        except sshconfig.UnsupportedOption as e:
            raise ConnectError(str(e)) from e
        jp = (hop.proxy_jump or "").strip().lower()
        if jp and jp != "none":
            raise ConnectError(
                f"踏み台 {host} 自体に ProxyJump が設定されています。"
                "入れ子の多段には未対応です。接続先の ProxyJump に"
                "カンマ区切りで平坦化してください。(黙って直接接続はしません)")
        if not hop.username:
            hop = replace(hop, username=profile.username)
        hops.append(hop)
    if len(hops) > MAX_JUMP_HOPS:
        raise ConnectError(
            f"ProxyJump の段数が多すぎます({len(hops)} 段 > 上限 {MAX_JUMP_HOPS})。")
    return hops


class SshSession:
    """1 接続 = 1 Transport。シェルと SFTP は同じ Transport 上の別チャネル。

    ProxyJump 指定時は踏み台ごとに Transport を張り、direct-tcpip チャネルを
    次のホップのソケット代わりに使って多段接続する(self.transport は常に最終目的地)。
    """

    def __init__(self, profile: Profile, known_hosts: KnownHosts | None = None):
        self.profile = profile
        self.known_hosts = known_hosts or KnownHosts()
        self.transport: paramiko.Transport | None = None
        self._jump_transports: list[paramiko.Transport] = []
        self.keepalive = 30  # Settings から上書き可能

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
        # ~/.ssh/config の Host エイリアス解決(TOFU・認証も解決後の値で行う)
        try:
            p = self.profile = sshconfig.resolve_profile(p)
        except sshconfig.UnsupportedOption as e:
            raise ConnectError(str(e)) from e
        if not p.username:
            raise ConnectError("ユーザー名を入力してください。")

        hops = resolve_jump_chain(p)
        targets = hops + [p]

        first = targets[0]
        label = "踏み台 " if hops else ""
        try:
            sock = socket.create_connection((first.host, first.port), timeout=10)
        except OSError as e:
            raise ConnectError(
                f"{label}{first.host}:{first.port} に接続できません ({e})") from e

        opened: list[paramiko.Transport] = []
        try:
            for i, target in enumerate(targets):
                t = paramiko.Transport(sock)
                t.set_keepalive(getattr(self, "keepalive", 30))
                try:
                    t.start_client(timeout=15)
                except paramiko.SSHException as e:
                    t.close()
                    raise ConnectError(
                        f"{target.host} との SSH ネゴシエーションに失敗しました ({e})"
                    ) from e
                opened.append(t)
                self._verify_host_key(t, ui, target.host, target.port)
                if target is p:
                    self._authenticate(t, ui)
                else:
                    self._auth_jump(t, ui, target)
                    nxt = targets[i + 1]
                    try:
                        sock = t.open_channel(
                            "direct-tcpip",
                            (nxt.host, nxt.port), ("127.0.0.1", 0))
                    except paramiko.SSHException as e:
                        raise ConnectError(
                            f"踏み台 {target.host} から {nxt.host}:{nxt.port} へ"
                            f"転送チャネルを開けません ({e})") from e
        except Exception:
            for t in reversed(opened):
                try:
                    t.close()
                except Exception:
                    logger.debug("transport.close() に失敗 (無視)", exc_info=True)
            raise

        self._jump_transports = opened[:-1]
        self.transport = opened[-1]

    def _verify_host_key(self, t: paramiko.Transport, ui,
                         host: str, port: int) -> None:
        server_key = t.get_remote_server_key()
        fp = fingerprint_sha256(server_key)
        key_type = server_key.get_name()
        status, old_fp = self.known_hosts.check(host, port, key_type, fp)
        if status == "match":
            return
        info = {
            "host": host,
            "port": port,
            "key_type": key_type,
            "fingerprint": fp,
            "status": status,          # "new" or "mismatch"
            "old_fingerprint": old_fp,
        }
        if not ui.confirm_hostkey(info):
            raise ConnectCancelled()
        self.known_hosts.remember(host, port, key_type, fp)

    def _authenticate(self, t: paramiko.Transport, ui) -> None:
        p = self.profile
        if p.auth_method == AUTH_KEY:
            self._auth_key(t, ui, p)
        elif p.auth_method == AUTH_AGENT:
            self._auth_agent(t)
        else:
            self._auth_password(t, ui, p)
        if not t.is_authenticated():
            raise ConnectError("認証に失敗しました。")

    def _auth_jump(self, t: paramiko.Transport, ui, hop: Profile) -> None:
        """踏み台の認証。鍵ファイル(config の IdentityFile)→ エージェント →
        パスワードの順に試す。プロンプトには必ず「踏み台」を含める
        (GUI 側が保存済みの接続先パスワードを踏み台に自動送信しないための目印)。
        """
        if hop.key_path:
            try:
                self._auth_key(t, ui, hop, label="踏み台 ")
            except ConnectError:
                logger.debug("踏み台 %s の鍵認証に失敗 (他の方式を試す)", hop.host)
        if not t.is_authenticated():
            try:
                agent_keys = paramiko.Agent().get_keys()
            except Exception:  # noqa: BLE001 - エージェント不在は普通にある
                agent_keys = ()
            for k in agent_keys:
                try:
                    t.auth_publickey(hop.username, k)
                    break
                except paramiko.AuthenticationException:
                    continue
        if not t.is_authenticated():
            self._auth_password(t, ui, hop, label="踏み台 ")
        if not t.is_authenticated():
            raise ConnectError(
                f"踏み台 {hop.username}@{hop.host} の認証に失敗しました。")

    def _auth_key(self, t: paramiko.Transport, ui, p: Profile,
                  label: str = "") -> None:
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
                    f"{label}秘密鍵のパスフレーズを入力\n({p.key_path})"
                )
                if passphrase is None:
                    raise ConnectCancelled()
            except (paramiko.SSHException, ConnectError, ValueError):
                # パスフレーズ違いで復号失敗した場合など
                passphrase = ui.get_secret(
                    f"{label}パスフレーズが違います。再入力してください\n({p.key_path})"
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

    def _auth_password(self, t: paramiko.Transport, ui, p: Profile,
                       label: str = "") -> None:
        for _ in range(3):
            password = ui.get_secret(
                f"{label}{p.username}@{p.host} のパスワードを入力")
            if password is None:
                raise ConnectCancelled()
            try:
                t.auth_password(p.username, password)
                return
            except paramiko.AuthenticationException:
                continue
        raise ConnectError(f"{label}パスワード認証に失敗しました。")

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
            logger.debug("exec_command 出力読み取り中に例外 (継続)", exc_info=True)
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
                # 送信失敗すると sudo はプロンプトで止まる/失敗する。rc/err で
                # 下流に伝わるが、原因追跡のため記録は残す。
                logger.warning("sudo パスワードの送信に失敗しました", exc_info=True)
        out, err = b"", b""
        try:
            while not ch.exit_status_ready():
                if ch.recv_ready():
                    out += ch.recv(65536)
                if ch.recv_stderr_ready():
                    err += ch.recv_stderr(65536)
        except Exception:
            logger.debug("run_sudo 出力読み取り中に例外 (継続)", exc_info=True)
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
                logger.debug("transport.close() に失敗 (無視)", exc_info=True)
            self.transport = None
        # 踏み台は目的地側から順に閉じる
        for t in reversed(self._jump_transports):
            try:
                t.close()
            except Exception:
                logger.debug("踏み台 transport.close() に失敗 (無視)", exc_info=True)
        self._jump_transports = []
