"""ランチャー強化(Issue #81: 検索・タグ/色・最終接続日時)のテスト。"""
import time

from hashi.config import Profile


def test_profile_backward_compat_and_roundtrip(tmp_path):
    """旧 profiles.json(新フィールド無し)がそのまま読める + 保存往復。"""
    from dataclasses import asdict

    old = {"name": "srv", "host": "h", "port": 22, "username": "u"}
    p = Profile.from_dict(old)
    assert p.tags == [] and p.color == "" and p.last_connected == 0.0

    p2 = Profile(name="x", tags=["本番", "web"], color="#e06c75",
                 last_connected=123.0)
    p3 = Profile.from_dict(asdict(p2))
    assert p3.tags == ["本番", "web"]
    assert p3.color == "#e06c75"
    assert p3.last_connected == 123.0


def test_relative_time_buckets(qapp):
    from hashi.mainwindow import _relative_time

    now = time.time()
    assert _relative_time(0) == ""
    assert _relative_time(now - 5) == "たった今"
    assert _relative_time(now - 300) == "5 分前"
    assert _relative_time(now - 7200) == "2 時間前"
    assert _relative_time(now - 86400 * 3) == "3 日前"
    assert _relative_time(now - 86400 * 90).count("-") == 2  # 日付表示


def test_launcher_order_filters_and_sorts(qapp):
    from hashi.mainwindow import _launcher_order

    profiles = [
        Profile(name="alpha", host="10.0.0.1", username="u",
                tags=["本番"], last_connected=100.0),
        Profile(name="beta", host="10.0.0.2", username="admin",
                last_connected=200.0),
        Profile(name="gamma", host="192.168.0.5", username="u"),
    ]
    # 検索なし: 最終接続の新しい順 → 未接続(label 順)
    order = [p.name for _i, p in _launcher_order(profiles)]
    assert order == ["beta", "alpha", "gamma"]
    # index は store 上の位置を保つ(フィルタしてもズレない)
    idxs = {p.name: i for i, p in _launcher_order(profiles, "u")}
    assert idxs["alpha"] == 0 and idxs["gamma"] == 2
    # タグ・ユーザー名でも絞れる(大文字小文字無視)
    assert [p.name for _i, p in _launcher_order(profiles, "本番")] == ["alpha"]
    assert [p.name for _i, p in _launcher_order(profiles, "ADMIN")] == ["beta"]
    assert [p.name for _i, p in _launcher_order(profiles, "192.168")] == ["gamma"]


def test_connect_dialog_tags_color_roundtrip(qapp):
    from hashi.dialogs import ConnectDialog

    src = Profile(name="srv", host="h", username="u",
                  tags=["本番", "web"], color="#61afef", last_connected=42.0)
    dlg = ConnectDialog(profile=src)
    assert dlg.ed_tags.text() == "本番, web"
    assert dlg.cb_color.currentData() == "#61afef"

    dlg.ed_tags.setText(" db ,  検証 , ")
    out = dlg.result_profile()
    assert out.tags == ["db", "検証"]
    assert out.color == "#61afef"
    assert out.last_connected == 42.0   # 編集しても接続履歴は消えない

    # 新規作成では空
    dlg2 = ConnectDialog()
    dlg2.ed_host.setText("h2")
    out2 = dlg2.result_profile()
    assert out2.tags == [] and out2.color == "" and out2.last_connected == 0.0
