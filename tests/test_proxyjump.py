"""ProxyJump(多段接続)のテスト。

- ユニット(CI 常時): 書式パース・踏み台チェーンの解決・「最初に踏み台へ接続しに
  行くこと」を到達不能ポートで確認する。ネットワーク不要。
- 結合(ライブ): 実 sshd が 2 台ある環境でのみ実行(HASHI_LIVE_SSH=1)。
  2026-07-11 の実機検証を再現可能にしたもの。
"""
import os

import pytest

from hashi.config import AUTH_PASSWORD, Profile
from hashi.ssh_core import (
    MAX_JUMP_HOPS,
    ConnectError,
    SshSession,
    parse_jump_specs,
    resolve_jump_chain,
)

# ---- parse_jump_specs -------------------------------------------------------

def test_parse_single_host():
    assert parse_jump_specs("bastion") == [("", "bastion", None)]


def test_parse_user_host_port():
    assert parse_jump_specs("ops@bastion:2222") == [("ops", "bastion", 2222)]


def test_parse_multi_hop_chain():
    assert parse_jump_specs("a@h1, h2:22 ,h3") == [
        ("a", "h1", None), ("", "h2", 22), ("", "h3", None)]


def test_parse_ipv6_bracketed():
    assert parse_jump_specs("u@[2001:db8::1]:2200") == [
        ("u", "2001:db8::1", 2200)]


def test_parse_ipv6_bare():
    # ブラケット無しでコロンが複数 = 裸の IPv6(ポート指定なし)
    assert parse_jump_specs("2001:db8::1") == [("", "2001:db8::1", None)]


def test_parse_username_with_at_mark():
    # user 部に @ を含む場合は最後の @ で区切る(OpenSSH と同じ)
    assert parse_jump_specs("user@example@bastion") == [
        ("user@example", "bastion", None)]


@pytest.mark.parametrize("bad", [
    "host:notaport", "host:0", "host:99999", "[::1", "[::1]x", "@:22",
])
def test_parse_invalid_specs_raise(bad):
    with pytest.raises(ConnectError):
        parse_jump_specs(bad)


# ---- resolve_jump_chain -----------------------------------------------------

def _patch_config(monkeypatch, tmp_path, text: str):
    import hashi.sshconfig as sc
    p = tmp_path / "config"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sc, "config_path", lambda: p)


def test_empty_or_none_spec_gives_no_hops(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path, "")
    assert resolve_jump_chain(Profile(host="x", username="u")) == []
    assert resolve_jump_chain(
        Profile(host="x", username="u", proxy_jump="none")) == []


def test_chain_order_and_username_fallback(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path, "")
    prof = Profile(host="dest", username="deploy",
                   proxy_jump="ops@b1:2200,b2")
    hops = resolve_jump_chain(prof)
    assert [(h.host, h.port, h.username) for h in hops] == [
        ("b1", 2200, "ops"),
        ("b2", 22, "deploy"),   # ユーザー名未指定は接続先のものを流用
    ]


def test_hop_alias_resolved_via_ssh_config(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path, """\
Host bastion
    HostName 198.51.100.1
    User gate
    Port 2222
    IdentityFile ~/.ssh/id_gate
""")
    hops = resolve_jump_chain(
        Profile(host="dest", username="u", proxy_jump="bastion"))
    assert len(hops) == 1
    h = hops[0]
    assert (h.host, h.port, h.username) == ("198.51.100.1", 2222, "gate")
    assert h.key_path.endswith("id_gate")


def test_nested_proxyjump_on_hop_is_rejected(monkeypatch, tmp_path):
    """踏み台自体の ProxyJump は入れ子未対応。黙って直結せずエラーにする。"""
    _patch_config(monkeypatch, tmp_path, """\
Host bastion
    HostName 198.51.100.1
    ProxyJump deeper
""")
    with pytest.raises(ConnectError, match="平坦化"):
        resolve_jump_chain(
            Profile(host="dest", username="u", proxy_jump="bastion"))


def test_too_many_hops_rejected(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path, "")
    spec = ",".join(f"h{i}" for i in range(MAX_JUMP_HOPS + 1))
    with pytest.raises(ConnectError, match="段数"):
        resolve_jump_chain(Profile(host="dest", username="u", proxy_jump=spec))


# ---- connect() が踏み台へ先に接続すること -----------------------------------

def test_connect_dials_first_hop_not_destination(monkeypatch, tmp_path):
    """ProxyJump 指定時、TCP 接続先は目的地ではなく最初の踏み台。"""
    _patch_config(monkeypatch, tmp_path, "")
    prof = Profile(host="unreachable.example.invalid", username="u",
                   auth_method=AUTH_PASSWORD, proxy_jump="127.0.0.1:1")
    sess = SshSession(prof)
    with pytest.raises(ConnectError) as ei:
        sess.connect(ui=None)  # ポート 1 は閉じているので TCP で失敗する
    msg = str(ei.value)
    assert "踏み台" in msg and "127.0.0.1:1" in msg
    assert "unreachable.example.invalid" not in msg


# ---- ライブ結合テスト(実 sshd が 2 台必要。実機検証を再現) -----------------
#
# 実行方法:
#   sshd を 127.0.0.1:2222(踏み台役)と 127.0.0.1:2223(目的地役)で起動し、
#   tester/testpass を用意した上で
#   HASHI_LIVE_SSH=1 QT_QPA_PLATFORM=offscreen pytest tests/test_proxyjump.py -k live

@pytest.mark.skipif(os.environ.get("HASHI_LIVE_SSH") != "1",
                    reason="実 sshd が 2 台必要(HASHI_LIVE_SSH=1 で有効化)")
def test_proxyjump_live_real_sshd(tmp_path, monkeypatch):
    from hashi.config import KnownHosts

    _patch_config(monkeypatch, tmp_path, "")
    user = os.environ.get("HASHI_LIVE_USER", "tester")
    bastion_port = int(os.environ.get("HASHI_LIVE_PORT", "2222"))
    dest_port = int(os.environ.get("HASHI_LIVE_PORT2", "2223"))

    class Ui:
        prompts: list[str] = []

        def get_secret(self, prompt):
            self.prompts.append(prompt)
            return os.environ.get("HASHI_LIVE_PASS", "testpass")

        def confirm_hostkey(self, info):
            return True

    prof = Profile(host="127.0.0.1", port=dest_port, username=user,
                   auth_method=AUTH_PASSWORD,
                   proxy_jump=f"{user}@127.0.0.1:{bastion_port}")
    kh = KnownHosts(path=tmp_path / "kh.json")
    sess = SshSession(prof, kh)
    ui = Ui()
    sess.connect(ui)
    try:
        # 踏み台のプロンプトには「踏み台」が含まれる(GUI 側の目印)
        assert any("踏み台" in p for p in ui.prompts)
        assert len(sess._jump_transports) == 1
        # 目的地へ届いていること(接続先ポートが SSH_CONNECTION に出る)
        rc, out, _ = sess.exec_command("echo $SSH_CONNECTION")
        assert rc == 0 and out.split()[3] == str(dest_port)
        # SFTP も最終 Transport 上で開ける
        sftp = sess.open_sftp()
        sftp.listdir(".")
        sftp.close()
        # 踏み台・目的地それぞれの TOFU 記録が残る
        assert f"127.0.0.1:{bastion_port}" in kh._data
        assert f"127.0.0.1:{dest_port}" in kh._data
    finally:
        sess.close()
    assert sess.transport is None and sess._jump_transports == []
