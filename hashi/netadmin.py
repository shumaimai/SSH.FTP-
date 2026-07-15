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
DROPIN_BASENAME = posixpath.basename(DROPIN_PATH)
SENTINEL = "/tmp/hashi-netplan-armed"
ROLLBACK_MARKER = "/tmp/hashi-netplan-rolledback"
DEFAULT_ROLLBACK_SEC = 20


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


def dropin_exists(session) -> bool:
    """既存の Hashi ドロップイン設定が残っているか。"""
    rc, _out, _err = session.exec_command(f"test -f {DROPIN_PATH}")
    return rc == 0


def _backup(session) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = f"/tmp/hashi-netplan-backup-{ts}.tgz"
    # 前回の Hashi ドロップインをバックアップに含めない( Issue #71)。
    # 復元は常に Hashi 導入前の構成 + ドロップイン削除となる。
    rc, _out, err = session.run_sudo(
        f"tar czf {dest} -C {NETPLAN_DIR} --exclude={DROPIN_BASENAME} .",
        _sudo_pw(session))
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


def _restore_script(backup: str, iface: str = "", address_cidr: str = "") -> str:
    """元設定へ戻す共通シェル断片。

    netplan apply は「いま設定した静的アドレス」を剥がしてくれない(実機で確認、
    Issue #61)ので、復元後に ip addr del で残留アドレスを明示的に消す。
    さらにロールバックが起きたことを ROLLBACK_MARKER に残し、GUI が次回
    「確定されず元へ戻った」ことをユーザーへ伝えられるようにする。
    """
    # ドロップインを tar 展開前後の両方で消す(古いバックアップに含まれる場合も
    # 含まれない場合も、必ず Hashi 管理外の構成に戻す: Issue #71)。
    parts = [f"rm -f {DROPIN_PATH}", f"tar xzf {backup} -C {NETPLAN_DIR}",
             f"rm -f {DROPIN_PATH}", "netplan apply"]
    if iface and address_cidr:
        parts.append(f"ip addr del {address_cidr} dev {iface} 2>/dev/null || true")
    parts.append(f"touch {ROLLBACK_MARKER}")
    parts.append(f"rm -f {SENTINEL}")
    return "; ".join(parts)


def _arm_rollback(session, backup: str, timeout: int,
                  iface: str = "", address_cidr: str = "") -> None:
    """番兵を作り、timeout 後に番兵が残っていれば元へ戻す nohup ジョブを起動。

    「20 秒以内に確定(disarm)がなければ元へ戻す」指示を、緩める前に
    サーバー側へ書き込んでから適用する(permjournal と同じ順序思想)。
    """
    restore = _restore_script(backup, iface, address_cidr)
    script = (
        f"touch {SENTINEL}; "
        f"nohup sh -c 'sleep {timeout}; "
        f"if [ -f {SENTINEL} ]; then {restore}; fi' >/dev/null 2>&1 &"
    )
    rc, _out, err = session.run_sudo(f"sh -c {_shq(script)}", _sudo_pw(session))
    if rc != 0:
        raise NetAdminError(
            f"自動ロールバックの仕掛けに失敗しました(適用を中止): {err.strip()}")


def _disarm(session) -> bool:
    """確定: 番兵を消す。

    番兵が既に無い(=タイマーが発火してロールバック済み)なら False を返す。
    確定とタイマー発火の競合を成功と誤認しないためのチェック。
    """
    rc, _out, _err = session.run_sudo(
        f"sh -c {_shq(f'test -f {SENTINEL} && rm -f {SENTINEL}')}",
        _sudo_pw(session))
    return rc == 0


def _rollback_now(session, backup: str,
                  iface: str = "", address_cidr: str = "") -> None:
    session.run_sudo(
        f"sh -c {_shq(_restore_script(backup, iface, address_cidr))}",
        _sudo_pw(session))


def consume_rollback_marker(session) -> bool:
    """前回のロールバック痕跡があれば消して True(GUI が通知に使う)。"""
    rc, _out, _err = session.run_sudo(
        f"sh -c {_shq(f'test -f {ROLLBACK_MARKER} && rm -f {ROLLBACK_MARKER}')}",
        _sudo_pw(session))
    return rc == 0


def current_gateway(session, iface: str = "") -> str:
    """デフォルトルートのゲートウェイを返す(無ければ空)。GUI の引き継ぎ用。"""
    dev = f" dev {iface}" if iface else ""
    rc, out, _err = session.exec_command(f"ip -4 route show default{dev}")
    if rc != 0:
        return ""
    for line in out.splitlines():
        parts = line.split()
        if "via" in parts:
            return parts[parts.index("via") + 1]
    return ""


def fallback_cleanup_addresses(session, iface: str, keep_cidr: str) -> list[str]:
    """新 IP への接続が取れない場合の残留アドレス掃除フォールバック。

    実行中の接続が切れる可能性があるため、削除コマンドを `nohup` + 1 秒 sleep
    でバックグラウンド化し、応答を待たずに確実にサーバー側で実行されるようにする。
    実際の削除結果は確認できない(接続が切れるため)ので、削除対象の CIDR 一覧を返す。
    """
    rc, out, err = session.exec_command(f"ip -o -4 addr show dev {iface}")
    if rc != 0:
        raise NetAdminError(f"アドレス一覧を取得できません: {err.strip()}")
    keep = str(ipaddress.ip_interface(keep_cidr).ip)
    to_delete = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" not in parts or "host" in parts:
            continue
        cidr = parts[parts.index("inet") + 1]
        if str(ipaddress.ip_interface(cidr).ip) == keep:
            continue
        to_delete.append(cidr)
    if not to_delete:
        return []
    deletions = "; ".join(
        f"ip addr del {c} dev {iface} 2>/dev/null || true" for c in to_delete)
    # 1 秒待ってから削除し、SSH 応答が返ってきて接続が閉じてから実行されるようにする。
    inner = f"sleep 1; {deletions}"
    nohup_cmd = f"nohup sh -c {_shq(inner)} >/dev/null 2>&1 &"
    script = f"sh -c {_shq(nohup_cmd)}"
    rc, _out, err = session.run_sudo(script, _sudo_pw(session))
    if rc != 0:
        raise NetAdminError(f"旧セッション経由の掃除に失敗しました: {err.strip()}")
    return to_delete


def cleanup_addresses(session, iface: str, keep_cidr: str) -> list[str]:
    """iface 上の IPv4 アドレスを keep_cidr 以外すべて削除する(残留 IP の掃除)。

    確定後に「新しい IP だけが載っている」状態にするための処理。旧 IP 経由の
    接続はここで切れるので、必ず**新 IP で張った接続**から呼ぶこと。
    """
    rc, out, err = session.exec_command(f"ip -o -4 addr show dev {iface}")
    if rc != 0:
        raise NetAdminError(f"アドレス一覧を取得できません: {err.strip()}")
    keep = str(ipaddress.ip_interface(keep_cidr).ip)
    removed = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" not in parts or "host" in parts:
            continue  # ループバック(scope host)は絶対に触らない
        cidr = parts[parts.index("inet") + 1]
        if str(ipaddress.ip_interface(cidr).ip) == keep:
            continue
        rc, _out, err = session.run_sudo(
            f"ip addr del {cidr} dev {iface}", _sudo_pw(session))
        if rc == 0:
            removed.append(cidr)
        else:
            logger.warning("残留アドレス %s の削除に失敗: %s", cidr, err.strip())
    return removed


def _remove_dropin(session) -> None:
    session.run_sudo(f"rm -f {DROPIN_PATH}", _sudo_pw(session))


def _shq(s: str) -> str:
    """シングルクォートで安全に包む(内側の ' は '\\'' に)。"""
    return "'" + s.replace("'", "'\\''") + "'"


def apply_static_ip(session, *, iface: str, address_cidr: str,
                    gateway: str = "", nameservers=None,
                    verify_reachable=None, post_confirm=None,
                    rollback_sec: int = DEFAULT_ROLLBACK_SEC) -> dict:
    """静的 IP を安全に適用する(netplan 限定 + 自動ロールバック)。

    verify_reachable(new_ip) は「新しい IP へ別接続して疎通するか」を返すコールバック。
    None のときは疎通確認をスキップ(自動ロールバックのタイマー任せ)。
    post_confirm(new_ip) は確定(disarm)後に呼ばれる後片付けフック。旧 IP の
    残留アドレス掃除(cleanup_addresses)に使う。旧 IP 経由の接続を切る操作を
    含むため、確定の前ではなく**後**に、新 IP 側の接続で行うこと。失敗しても
    適用自体は成功として扱う(戻り値 "cleaned" / "cleanup_note" に結果を入れる)。
    post_confirm は削除した CIDR の list、あるいは {"removed": [...], "note": str}
    を返せる。
    returns {"backup", "dropin", "confirmed", "new_ip", "iface", "cleaned", "cleanup_note"}。
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
    _arm_rollback(session, backup, rollback_sec, iface, address_cidr)

    rc, _out, err = session.run_sudo("netplan apply", _sudo_pw(session))
    if rc != 0:
        _rollback_now(session, backup, iface, address_cidr)
        raise NetAdminError(f"netplan apply に失敗しました(元へ戻しました): {err.strip()}")

    new_ip = str(ipaddress.ip_interface(address_cidr).ip)
    confirmed = True
    if verify_reachable is not None:
        confirmed = bool(verify_reachable(new_ip))
        if not confirmed:
            _rollback_now(session, backup, iface, address_cidr)
            raise NetAdminError(
                "新しい IP への疎通確認に失敗しました。設定を元に戻しました"
                f"(残っていても {rollback_sec} 秒で自動復帰します)。\n"
                "アドレス/ゲートウェイ/経路の指定を確認してください。")
    if not _disarm(session):
        # 疎通確認より先にタイマーが発火 = すでに元へ戻っている
        raise NetAdminError(
            f"確定より先に自動ロールバック({rollback_sec} 秒)が作動したため、"
            "設定は元に戻りました。ロールバック秒数を増やして再試行してください。")
    cleaned = []
    cleanup_note = ""
    if post_confirm is not None:
        try:
            pc = post_confirm(new_ip)
            if isinstance(pc, dict):
                cleaned = pc.get("removed") or []
                cleanup_note = pc.get("note", "")
            else:
                cleaned = pc or []
        except Exception:  # noqa: BLE001 掃除の失敗で適用成功を覆さない
            logger.warning("確定後の残留アドレス掃除に失敗", exc_info=True)
    return {"backup": backup, "dropin": DROPIN_PATH, "confirmed": confirmed,
            "new_ip": new_ip, "iface": iface, "cleaned": cleaned,
            "cleanup_note": cleanup_note}
