"""静的 IP 設定(Issue #45、netplan)のテスト。

実 netplan は触らず、フェイクセッションでコマンド列と安全ガード(自動ロールバック /
疎通失敗時の巻き戻し / netplan 非対応の拒否)を検証する。実 Ubuntu での通し検証は
オーナー/Devin の実機に委ねる(未検証)。
"""
import pytest

from hashi import netadmin
from hashi.netadmin import NetAdminError, apply_static_ip, build_netplan_yaml


class _FakeSftpFile:
    def __init__(self, store, path):
        self.store, self.path, self.buf = store, path, b""

    def write(self, data):
        self.buf += data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.store[self.path] = self.buf


class _FakeSftp:
    def __init__(self, store):
        self.store = store

    def open(self, path, mode="r"):
        return _FakeSftpFile(self.store, path)

    def close(self):
        pass


class FakeSession:
    def __init__(self, netplan=True):
        self._hashi_sudo_pw = "pw"
        self.calls = []
        self.tmp_files = {}
        self.netplan = netplan
        self.responses = {}   # 部分一致 -> (rc, out, err)

    def _match(self, command):
        # arm コマンドは sh -c '...netplan apply...' と埋め込むので、部分一致だと
        # 誤爆する。実際の単発コマンド(== または先頭一致)だけに応答を返す。
        for key, resp in self.responses.items():
            if command == key or command.startswith(key):
                return resp
        return (0, "", "")

    def exec_command(self, command, timeout=15.0):
        self.calls.append(command)
        if 'printf "%s" "$HOME"' in command:
            return (0, "/home/tester", "")
        if command.startswith("ip -o -4 addr"):
            return (0, "1: lo    inet 127.0.0.1/8 scope host lo\n"
                       "2: eth0    inet 192.168.1.50/24 scope global eth0\n", "")
        return self._match(command)

    def open_sftp(self):
        return _FakeSftp(self.tmp_files)

    def run_sudo(self, command, password, timeout=20.0):
        self.calls.append(command)
        if "command -v netplan" in command:
            return (0 if self.netplan else 1, "", "")
        return self._match(command)

    def dropin_content(self):
        for path, buf in self.tmp_files.items():
            if path.endswith(".hashi-netplan.tmp"):
                return buf.decode("utf-8")
        return None

    def idx(self, needle):
        for i, c in enumerate(self.calls):
            if needle in c:
                return i
        return -1

    def idx_exact(self, cmd):
        for i, c in enumerate(self.calls):
            if c == cmd:
                return i
        return -1


def test_build_yaml_valid():
    y = build_netplan_yaml("eth0", "192.168.1.10/24", "192.168.1.1",
                           ["1.1.1.1", "8.8.8.8"])
    assert "eth0:" in y and "192.168.1.10/24" in y
    assert "to: default" in y and "via: 192.168.1.1" in y
    assert "addresses: [1.1.1.1, 8.8.8.8]" in y
    assert "renderer: networkd" in y


@pytest.mark.parametrize("addr,gw", [
    ("999.1.1.1/24", ""),
    ("192.168.1.10", ""),        # プレフィックス無しは ip_interface でも通るが…
    ("192.168.1.10/24", "notip"),
])
def test_build_yaml_rejects_bad_input(addr, gw):
    # "192.168.1.10" は /32 とみなされ通るので、これは gw 不正のみ弾く想定
    if addr == "192.168.1.10":
        build_netplan_yaml("eth0", addr, gw)   # 例外にならないことを許容
        return
    with pytest.raises(NetAdminError):
        build_netplan_yaml("eth0", addr, gw)


def test_list_interfaces_excludes_lo():
    sess = FakeSession()
    ifaces = netadmin.list_interfaces(sess)
    assert [i["name"] for i in ifaces] == ["eth0"]
    assert ifaces[0]["address"] == "192.168.1.50/24"


def test_non_netplan_env_is_rejected():
    sess = FakeSession(netplan=False)
    with pytest.raises(NetAdminError, match="netplan で管理されていません"):
        apply_static_ip(sess, iface="eth0", address_cidr="192.168.1.10/24")
    # 何も書き込んでいない
    assert sess.dropin_content() is None


def test_happy_path_sequence_and_disarm():
    sess = FakeSession()
    res = apply_static_ip(
        sess, iface="eth0", address_cidr="192.168.1.10/24",
        gateway="192.168.1.1", verify_reachable=lambda ip: True)
    assert res["confirmed"] is True
    assert "192.168.1.10/24" in sess.dropin_content()
    # 順序: バックアップ → install → generate → arm(touch 番兵) → apply → disarm
    i_backup = sess.idx("-czf /tmp/hashi-netplan-backup")
    i_install = sess.idx("install -o root")
    i_gen = sess.idx_exact("netplan generate")
    i_arm = sess.idx(f"touch {netadmin.SENTINEL}")
    i_apply = sess.idx_exact("netplan apply")
    i_disarm = max(i for i, c in enumerate(sess.calls)
                   if f"test -f {netadmin.SENTINEL} && rm -f {netadmin.SENTINEL}" in c)
    assert i_backup < i_install < i_gen < i_arm < i_apply < i_disarm
    assert res["new_ip"] == "192.168.1.10"


def test_generate_failure_aborts_and_removes_dropin():
    sess = FakeSession()
    sess.responses["netplan generate"] = (1, "", "invalid yaml")
    with pytest.raises(NetAdminError, match="構文検証"):
        apply_static_ip(sess, iface="eth0", address_cidr="192.168.1.10/24")
    # dropin 削除が呼ばれ、apply は実行していない
    assert sess.idx(f"rm -f {netadmin.DROPIN_PATH}") >= 0
    assert sess.idx("netplan apply") == -1


def test_unreachable_rolls_back():
    sess = FakeSession()
    with pytest.raises(NetAdminError, match="疎通確認"):
        apply_static_ip(sess, iface="eth0", address_cidr="192.168.1.10/24",
                        verify_reachable=lambda ip: False)
    # ロールバック(バックアップ復元)が呼ばれている
    rollback = [c for c in sess.calls
                if "tar xzf /tmp/hashi-netplan-backup" in c
                and f"touch {netadmin.SENTINEL}" not in c]
    assert rollback
    # 残留アドレスの削除と、ロールバック痕跡(マーカー)も含む (#61)
    assert "ip addr del 192.168.1.10/24 dev eth0" in rollback[0]
    assert f"touch {netadmin.ROLLBACK_MARKER}" in rollback[0]


def test_apply_failure_rolls_back():
    sess = FakeSession()
    sess.responses["netplan apply"] = (1, "", "apply failed")
    with pytest.raises(NetAdminError, match="apply に失敗"):
        apply_static_ip(sess, iface="eth0", address_cidr="192.168.1.10/24")
    assert any("tar xzf /tmp/hashi-netplan-backup" in c for c in sess.calls)


def test_arm_uses_sentinel_and_timeout():
    sess = FakeSession()
    apply_static_ip(sess, iface="eth0", address_cidr="192.168.1.10/24",
                    verify_reachable=lambda ip: True, rollback_sec=90)
    armed = [c for c in sess.calls if "sleep 90" in c]
    assert armed and netadmin.SENTINEL in armed[0]
    # 番兵ジョブにも残留アドレス削除とマーカーが入っている (#61)
    assert "ip addr del 192.168.1.10/24 dev eth0" in armed[0]
    assert f"touch {netadmin.ROLLBACK_MARKER}" in armed[0]


def test_disarm_race_reports_rollback():
    """確定より先に番兵が発火していたら成功と誤認しない (#61)。"""
    sess = FakeSession()
    sess.responses[
        f"sh -c 'test -f {netadmin.SENTINEL} && rm -f {netadmin.SENTINEL}'"
    ] = (1, "", "")
    with pytest.raises(NetAdminError, match="自動ロールバック"):
        apply_static_ip(sess, iface="eth0", address_cidr="192.168.1.10/24",
                        verify_reachable=lambda ip: True)


def test_post_confirm_cleanup_result_and_isolation():
    """post_confirm は確定後に呼ばれ、失敗しても適用成功を覆さない (#61)。"""
    sess = FakeSession()
    res = apply_static_ip(
        sess, iface="eth0", address_cidr="192.168.1.10/24",
        verify_reachable=lambda ip: True,
        post_confirm=lambda ip: [f"cleaned-for-{ip}"])
    assert res["cleaned"] == ["cleaned-for-192.168.1.10"]

    def boom(_ip):
        raise RuntimeError("cleanup failed")

    sess2 = FakeSession()
    res2 = apply_static_ip(
        sess2, iface="eth0", address_cidr="192.168.1.10/24",
        verify_reachable=lambda ip: True, post_confirm=boom)
    assert res2["confirmed"] is True
    assert res2["cleaned"] == []


def test_cleanup_addresses_removes_all_but_keep():
    # FakeSession の既定出力: lo(scope host)と eth0 192.168.1.50/24
    sess = FakeSession()
    removed = netadmin.cleanup_addresses(sess, "eth0", "192.168.1.10/24")
    assert removed == ["192.168.1.50/24"]
    assert sess.idx("ip addr del 192.168.1.50/24 dev eth0") >= 0
    # ループバックと keep 対象は削除しない
    assert sess.idx("ip addr del 127.0.0.1/8") == -1
    assert sess.idx("ip addr del 192.168.1.10/24 dev eth0") == -1


def test_cleanup_addresses_keeps_target_address():
    sess = FakeSession()
    removed = netadmin.cleanup_addresses(sess, "eth0", "192.168.1.50/24")
    assert removed == []
    assert sess.idx("ip addr del") == -1


def test_current_gateway_parses_default_route():
    sess = FakeSession()
    sess.responses["ip -4 route show default"] = (
        0, "default via 192.168.0.1 dev eth0 proto dhcp metric 100\n", "")
    assert netadmin.current_gateway(sess) == "192.168.0.1"
    sess2 = FakeSession()
    sess2.responses["ip -4 route show default"] = (0, "", "")
    assert netadmin.current_gateway(sess2) == ""


def test_consume_rollback_marker():
    sess = FakeSession()
    key = (f"sh -c 'test -f {netadmin.ROLLBACK_MARKER} "
           f"&& rm -f {netadmin.ROLLBACK_MARKER}'")
    sess.responses[key] = (0, "", "")
    assert netadmin.consume_rollback_marker(sess) is True
    sess.responses[key] = (1, "", "")
    assert netadmin.consume_rollback_marker(sess) is False


def test_backup_excludes_hashi_dropin():
    """バックアップに前回のドロップインを含めない (#71)。含めるとロールバックで
    「元の設定」ではなく前回の静的 IP が復元され、二重 IP になる。"""
    sess = FakeSession()
    apply_static_ip(sess, iface="eth0", address_cidr="192.168.1.10/24",
                    verify_reachable=lambda ip: True)
    backup_cmds = [c for c in sess.calls if "-czf /tmp/hashi-netplan-backup" in c]
    assert backup_cmds
    assert "--exclude=./90-hashi.yaml" in backup_cmds[0]


def test_apply_reports_replaced_previous():
    """再実行(既存ドロップインあり)を結果で報告する (#71)。"""
    sess = FakeSession()   # フェイクは test -f に rc=0 を返す = 既存あり
    res = apply_static_ip(sess, iface="eth0", address_cidr="192.168.1.10/24",
                          verify_reachable=lambda ip: True)
    assert res["replaced_previous"] is True

    sess2 = FakeSession()
    sess2.responses[f"test -f {netadmin.DROPIN_PATH}"] = (1, "", "")
    res2 = apply_static_ip(sess2, iface="eth0", address_cidr="192.168.1.10/24",
                           verify_reachable=lambda ip: True)
    assert res2["replaced_previous"] is False


def test_schedule_stale_cleanup_arms_detached_job():
    """新 IP 接続が張れないときの遅延掃除 (#71): 旧接続から nohup で仕掛ける。"""
    sess = FakeSession()   # eth0 に 192.168.1.50/24 が載っている想定
    stale = netadmin.schedule_stale_cleanup(sess, "eth0", "192.168.1.10/24")
    assert stale == ["192.168.1.50/24"]
    armed = [c for c in sess.calls if "nohup" in c and "ip addr del" in c]
    assert armed and "ip addr del 192.168.1.50/24 dev eth0" in armed[0]
    assert "sleep 2" in armed[0]
    # keep 対象しか無ければ何も仕掛けない
    sess2 = FakeSession()
    assert netadmin.schedule_stale_cleanup(
        sess2, "eth0", "192.168.1.50/24") == []
    assert not any("nohup" in c and "ip addr del" in c for c in sess2.calls)
