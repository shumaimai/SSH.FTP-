"""静的 IP 設定の GUI 配線(Issue #45)のテスト。

ダイアログの入出力と、NetAdminWorker が netadmin.apply_static_ip を正しい引数で
呼ぶことを offscreen で確認する。実適用はフェイクセッションで代替(実 netplan は
オーナー/Devin の実機に委ねる)。
"""


def test_netadmin_dialog_roundtrip(qapp):
    from hashi.dialogs import NetAdminDialog

    ifaces = [{"name": "eth0", "address": "192.168.1.50/24"},
              {"name": "eth1", "address": "10.0.0.2/24"}]
    dlg = NetAdminDialog(interfaces=ifaces)
    dlg.cb_iface.setCurrentIndex(0)
    dlg.ed_address.setText("192.168.1.80/24")
    dlg.ed_gateway.setText("192.168.1.1")
    dlg.ed_dns.setText("1.1.1.1, 8.8.8.8")
    dlg.sp_rollback.setValue(90)
    cfg = dlg.result_settings()
    assert cfg == {
        "iface": "eth0",
        "address_cidr": "192.168.1.80/24",
        "gateway": "192.168.1.1",
        "nameservers": ["1.1.1.1", "8.8.8.8"],
        "rollback_sec": 90,
    }


def test_netadmin_dialog_editable_iface(qapp):
    from hashi.dialogs import NetAdminDialog

    dlg = NetAdminDialog(interfaces=[])
    dlg.cb_iface.setCurrentText("ens3")
    dlg.ed_address.setText("10.1.2.3/24")
    cfg = dlg.result_settings()
    assert cfg["iface"] == "ens3"
    # DNS は既定で 1.1.1.1 をプリフィル(#61)。消せば空にできる
    assert cfg["nameservers"] == ["1.1.1.1"]
    dlg.ed_dns.clear()
    assert dlg.result_settings()["nameservers"] == []


def test_netadmin_dialog_prefills_gateway_and_dns(qapp):
    from hashi.dialogs import NetAdminDialog

    dlg = NetAdminDialog(interfaces=[], default_gateway="192.168.0.1",
                         default_dns="9.9.9.9")
    assert dlg.ed_gateway.text() == "192.168.0.1"
    assert dlg.ed_dns.text() == "9.9.9.9"
    assert dlg.sp_rollback.value() == 20


def test_netadmin_worker_calls_apply_with_settings(qapp, monkeypatch):
    import hashi.mainwindow as mw
    from hashi.config import Profile

    captured = {}

    def fake_apply(session, **kwargs):
        captured.update(kwargs)
        captured["verify_result"] = kwargs["verify_reachable"]("192.168.1.80")
        return {"backup": "/tmp/bk.tgz", "dropin": "/etc/netplan/90-hashi.yaml",
                "confirmed": True}

    monkeypatch.setattr(mw.netadmin, "apply_static_ip", fake_apply)

    # 新 IP への疎通確認は SshSession.connect を差し替えて成功扱いにする
    class _Sess:
        def __init__(self, *a, **k):
            pass

        def connect(self, ui):
            pass

        def is_alive(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(mw, "SshSession", _Sess)

    cfg = {"iface": "eth0", "address_cidr": "192.168.1.80/24",
           "gateway": "192.168.1.1", "nameservers": ["1.1.1.1"],
           "rollback_sec": 120}

    class _CurSess:
        _hashi_sudo_pw = None

    worker = mw.NetAdminWorker(_CurSess(), Profile(host="192.168.1.50",
                              username="u"), None, None, "sudo-pw", cfg)
    results = []
    worker.ok.connect(results.append)
    worker.run()   # スレッドを起こさず同期実行

    assert captured["iface"] == "eth0"
    assert captured["address_cidr"] == "192.168.1.80/24"
    assert captured["gateway"] == "192.168.1.1"
    assert captured["nameservers"] == ["1.1.1.1"]
    assert captured["rollback_sec"] == 120
    assert captured["verify_result"] is True   # verify_reachable が疎通成功を返す
    assert results and results[0]["confirmed"] is True


def test_netadmin_worker_reports_error(qapp, monkeypatch):
    import hashi.mainwindow as mw
    from hashi.config import Profile

    def fake_apply(session, **kwargs):
        raise mw.netadmin.NetAdminError("疎通確認に失敗しました")

    monkeypatch.setattr(mw.netadmin, "apply_static_ip", fake_apply)
    cfg = {"iface": "eth0", "address_cidr": "192.168.1.80/24", "gateway": "",
           "nameservers": [], "rollback_sec": 120}

    class _CurSess:
        _hashi_sudo_pw = None

    worker = mw.NetAdminWorker(_CurSess(), Profile(host="h", username="u"),
                               None, None, "pw", cfg)
    fails = []
    worker.fail.connect(fails.append)
    worker.run()
    assert fails == ["疎通確認に失敗しました"]
