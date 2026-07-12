"""サーバーの静的 IP 設定(Issue #45): netplan 限定 + 自動ロールバック。

ネットワーク設定の失敗は **SSH ごと切断 → 復旧は物理/VM コンソール作業** という、
sshd 設定より重いダメージになる。そこで:

1. **netplan 以外は触らない**。`netplan` コマンドと `/etc/netplan/*.yaml` が無ければ
   明示エラー(NetworkManager / ifupdown / systemd-networkd を黙って編集しない)。
2. **自動ロールバック**: 新設定を適用する前に「一定時間内に『確定』の合図が来なければ
   自動で元へ戻す」バックグラウンドジョブを仕掛ける。`netplan try` は TTY を要求して
   SSH の exec では使いにくいので、番兵ファイルを見張る sleep ジョブで同等のことをする。
   - 適用前: バックアップ + 番兵ファイル作成 + 「sleep N 後に番兵が残っていれば元へ戻す」
     ジョブを nohup で起動。
   - 適用: `netplan apply`(この瞬間に現在の接続が切れることがある)。
   - 確認: 呼び出し側が **新しい IP へ別接続** して疎通を確かめる(verify_reachable)。
     - 成功 → 番兵を消して「確定」(ロールバックジョブは何もせず終わる)。
     - 失敗/例外 → 即ロールバック(バックアップ復元 + netplan apply)。番兵も消す。

GUI 非依存。session は SshSession 互換(exec_command / run_sudo / open_sftp)。
このコンテナには netplan が無く実適用は自セッションを切る危険があるため、
本モジュールはフェイクセッションのユニットテストまで。実 Ubuntu での通し検証は
オーナー / Devin の実機に委ねる(未検証を未検証と明示する)。
"""
from __future__ import annotations

import ipaddress
import logging
import posixpath
import time

logger = logging.getLogger(__name__)

NETPLAN_DIR = "/etc/netplan"
DROPIN_PATH = f"{NETPLAN_DIR}/90-hashi.yaml"
SENTINEL = "/tmp/hashi-netplan-armed"
DEFAULT_ROLLBACK_SEC = 120


class NetAdminError(Exception):
    """ネットワーク設定変更の失敗(メッセージはそのまま表示できる日本語)。"""


def _sudo_pw(session):
    return getattr(session, "_hashi_sudo_pw", None)


def detect_netplan(session) -> bool:
    """netplan で管理されている環境か(コマンド存在 + 設定ファイルあり)。"""
    rc, _out, _err = session.run_sudo(
        "sh -c 'command -v netplan >/dev/null "
        f"&& ls {NETPLAN_DIR}/*.yaml >/dev/null 2>&1'", _sudo_pw(session))
    return rc == 0


def list_interfaces(session) -> list[dict]:
    """`ip -o -4 addr` から (name, address) の一覧を作る(ループバック除く)。"""
    rc, out, err = session.exec_command("ip -o -4 addr show")
    if rc != 0:
        raise NetAdminError(f"インターフェース一覧を取得できません: {err.strip()}")
    result = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        name, addr = parts[1], parts[3]
        if name == "lo":
            continue
        result.append({"name": name, "address": addr})
    return result


def _validate(address_cidr: str, gateway: str, nameservers) -> None:
    try:
        ipaddress.ip_interface(address_cidr)   # "192.168.1.10/24"
    except ValueError as e:
        raise NetAdminError(
            f"IP アドレス/プレフィックスが不正です: {address_cidr}") from e
    if gateway:
        try:
            ipaddress.ip_address(gateway)
        except ValueError as e:
            raise NetAdminError(f"ゲートウェイが不正です: {gateway}") from e
    for ns in nameservers or []:
        try:
            ipaddress.ip_address(ns)
        except ValueError as e:
            raise NetAdminError(f"DNS が不正です: {ns}") from e


def build_netplan_yaml(iface: str, address_cidr: str, gateway: str = "",
                       nameservers=None) -> str:
    """静的 IP のドロップイン netplan(networkd renderer)を組み立てる。"""
    _validate(address_cidr, gateway, nameservers)
    lines = [
        "# Managed by Hashi (Issue #45). 消せば DHCP 等の元設定に戻ります。",
        "network:",
        "  version: 2",
        "  renderer: networkd",
        "  ethernets:",
        f"    {iface}:",
        "      dhcp4: false",
        "      addresses:",
        f"        - {address_cidr}",
    ]
    if gateway:
        # routes 形式(新しい netplan は gateway4 を非推奨にしている)
        lines += ["      routes:",
                  "        - to: default",
                  f"          via: {gateway}"]
    if nameservers:
        lines.append("      nameservers:")
        lines.append("        addresses: [{}]".format(
            ", ".join(nameservers)))
    return "\n".join(lines) + "\n"


def _backup(session) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = f"/tmp/hashi-netplan-backup-{ts}.tgz"
    rc, _out, err = session.run_sudo(
        f"tar czf {dest} -C {NETPLAN_DIR} .", _sudo_pw(session))
    if rc != 0:
        raise NetAdminError(f"netplan 設定のバックアップに失敗しました: {err.strip()}")
    return dest


def _write_dropin(session, content: str) -> None:
    """SFTP でホームに一時書き込み → sudo install(内容を sudo stdin に通さない)。

    権限は 600(netplan は world-readable な設定に警告を出すため)。
    """
    rc, home, _ = session.exec_command('printf "%s" "$HOME"')
    home = home.strip()
    if rc != 0 or not home.startswith("/"):
        raise NetAdminError("ホームディレクトリを取得できませんでした。")
    tmp = posixpath.join(home, ".hashi-netplan.tmp")
    sftp = session.open_sftp()
    try:
        with sftp.open(tmp, "wb") as f:
            f.write(content.encode("utf-8"))
    except OSError as e:
        raise NetAdminError(f"一時ファイルの書き込みに失敗しました: {e}") from e
    finally:
        try:
            sftp.close()
        except Exception:
            logger.debug("SFTP クローズに失敗 (無視)", exc_info=True)
    try:
        rc, _out, err = session.run_sudo(
            f"install -o root -g root -m 600 {tmp} {DROPIN_PATH}",
            _sudo_pw(session))
        if rc != 0:
            raise NetAdminError(f"設定ファイルの配置に失敗しました: {err.strip()}")
    finally:
        session.run_sudo(f"rm -f {tmp}", _sudo_pw(session))


def _generate(session) -> None:
    rc, _out, err = session.run_sudo("netplan generate", _sudo_pw(session))
    if rc != 0:
        _remove_dropin(session)
        raise NetAdminError(
            "netplan 設定の生成(構文検証)に失敗しました(適用を中止)。\n"
            f"{err.strip()}")


def _arm_rollback(session, backup: str, timeout: int) -> None:
    """番兵を作り、timeout 後に番兵が残っていれば元へ戻す nohup ジョブを起動。"""
    script = (
        f"touch {SENTINEL}; "
        f"nohup sh -c 'sleep {timeout}; "
        f"if [ -f {SENTINEL} ]; then "
        f"rm -f {DROPIN_PATH}; tar xzf {backup} -C {NETPLAN_DIR}; "
        f"netplan apply; rm -f {SENTINEL}; fi' >/dev/null 2>&1 &"
    )
    rc, _out, err = session.run_sudo(f"sh -c {_shq(script)}", _sudo_pw(session))
    if rc != 0:
        raise NetAdminError(
            f"自動ロールバックの仕掛けに失敗しました(適用を中止): {err.strip()}")


def _disarm(session) -> None:
    """確定: 番兵を消す(ロールバックジョブは sleep 明けに何もせず終わる)。"""
    session.run_sudo(f"rm -f {SENTINEL}", _sudo_pw(session))


def _rollback_now(session, backup: str) -> None:
    session.run_sudo(
        f"sh -c {_shq(f'rm -f {DROPIN_PATH}; tar xzf {backup} -C {NETPLAN_DIR}; netplan apply; rm -f {SENTINEL}')}",
        _sudo_pw(session))


def _remove_dropin(session) -> None:
    session.run_sudo(f"rm -f {DROPIN_PATH}", _sudo_pw(session))


def _shq(s: str) -> str:
    """シングルクォートで安全に包む(内側の ' は '\\'' に)。"""
    return "'" + s.replace("'", "'\\''") + "'"


def apply_static_ip(session, *, iface: str, address_cidr: str,
                    gateway: str = "", nameservers=None,
                    verify_reachable=None,
                    rollback_sec: int = DEFAULT_ROLLBACK_SEC) -> dict:
    """静的 IP を安全に適用する(netplan 限定 + 自動ロールバック)。

    verify_reachable(new_ip) は「新しい IP へ別接続して疎通するか」を返すコールバック。
    None のときは疎通確認をスキップ(自動ロールバックのタイマー任せ)。
    returns {"backup": path, "dropin": path, "confirmed": bool}。
    """
    _validate(address_cidr, gateway, nameservers)
    if not detect_netplan(session):
        raise NetAdminError(
            "この環境は netplan で管理されていません"
            f"(netplan コマンドまたは {NETPLAN_DIR}/*.yaml が見つからない)。"
            "安全に自動編集できないため中止しました。")

    backup = _backup(session)
    content = build_netplan_yaml(iface, address_cidr, gateway, nameservers)
    _write_dropin(session, content)
    _generate(session)                       # 構文 NG ならここで中止(dropin 削除済み)
    _arm_rollback(session, backup, rollback_sec)

    rc, _out, err = session.run_sudo("netplan apply", _sudo_pw(session))
    if rc != 0:
        _rollback_now(session, backup)
        raise NetAdminError(f"netplan apply に失敗しました(元へ戻しました): {err.strip()}")

    confirmed = True
    if verify_reachable is not None:
        new_ip = str(ipaddress.ip_interface(address_cidr).ip)
        confirmed = bool(verify_reachable(new_ip))
        if not confirmed:
            _rollback_now(session, backup)
            raise NetAdminError(
                "新しい IP への疎通確認に失敗しました。設定を元に戻しました。\n"
                "アドレス/ゲートウェイ/経路の指定を確認してください。")
    _disarm(session)
    return {"backup": backup, "dropin": DROPIN_PATH, "confirmed": confirmed}
