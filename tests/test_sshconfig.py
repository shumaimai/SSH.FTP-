"""sshconfig.py(~/.ssh/config の Host エイリアス解決)のテスト(Issue #3)。"""
import pytest

from hashi.config import AUTH_KEY, AUTH_PASSWORD, Profile
from hashi.sshconfig import UnsupportedOption, resolve_profile

_CONFIG = """\
Host myserver
    HostName 203.0.113.10
    User deploy
    Port 2222
    IdentityFile ~/.ssh/id_deploy

Host jump-needed
    HostName 10.0.0.5
    ProxyJump bastion.example.com

Host proxied
    HostName 10.0.0.6
    ProxyCommand ssh -W %h:%p bastion

Host no-proxy
    HostName 10.0.0.7
    User direct
    ProxyJump none

Host *
    User fallback
"""


@pytest.fixture()
def cfg(tmp_path):
    p = tmp_path / "config"
    p.write_text(_CONFIG, encoding="utf-8")
    return p


def test_alias_resolves_all_fields(cfg):
    prof = Profile(host="myserver", auth_method=AUTH_KEY)
    r = resolve_profile(prof, cfg)
    assert r.host == "203.0.113.10"
    assert r.port == 2222
    assert r.username == "deploy"
    assert r.key_path.endswith("id_deploy") and "~" not in r.key_path
    # 元の Profile は変更されない(コピーが返る)
    assert prof.host == "myserver" and prof.username == ""


def test_explicit_profile_values_win(cfg):
    prof = Profile(host="myserver", username="admin", port=2200,
                   auth_method=AUTH_KEY, key_path="/x/id_other")
    r = resolve_profile(prof, cfg)
    assert r.host == "203.0.113.10"      # HostName だけは常に解決
    assert r.username == "admin"          # 入力済みが優先
    assert r.port == 2200                 # 22 以外は明示指定とみなす
    assert r.key_path == "/x/id_other"


def test_identityfile_ignored_for_password_auth(cfg):
    prof = Profile(host="myserver", auth_method=AUTH_PASSWORD)
    r = resolve_profile(prof, cfg)
    assert r.key_path == ""


def test_unknown_host_gets_wildcard_defaults(cfg):
    prof = Profile(host="203.0.113.99")
    r = resolve_profile(prof, cfg)
    assert r.host == "203.0.113.99"
    assert r.username == "fallback"       # Host * の User


def test_missing_config_returns_profile_as_is(tmp_path):
    prof = Profile(host="myserver", username="u")
    assert resolve_profile(prof, tmp_path / "nonexistent") is prof


def test_proxyjump_is_rejected_not_ignored(cfg):
    """ProxyJump を黙って無視して直接接続してはいけない。"""
    with pytest.raises(UnsupportedOption):
        resolve_profile(Profile(host="jump-needed", username="u"), cfg)
    with pytest.raises(UnsupportedOption):
        resolve_profile(Profile(host="proxied", username="u"), cfg)


def test_proxyjump_none_is_allowed(cfg):
    """ProxyJump none は「プロキシを打ち消す」正規指定なので拒否しない。"""
    r = resolve_profile(Profile(host="no-proxy"), cfg)
    assert r.host == "10.0.0.7"
    assert r.username == "direct"


def test_ssh_core_uses_resolution(tmp_path, monkeypatch):
    """SshSession.connect が解決を通ることを確認(到達不能ホストで打ち切り)。"""
    import hashi.sshconfig as sc
    from hashi.ssh_core import ConnectError, SshSession

    p = tmp_path / "config"
    p.write_text("Host alias\n    HostName 127.0.0.1\n    Port 1\n    User u\n",
                 encoding="utf-8")
    monkeypatch.setattr(sc, "config_path", lambda: p)

    sess = SshSession(Profile(host="alias", auth_method=AUTH_PASSWORD))
    with pytest.raises(ConnectError) as ei:
        sess.connect(ui=None)  # ポート 1 は閉じているので TCP で失敗する
    # エイリアスではなく解決後の 127.0.0.1:1 へ接続しようとしたこと
    assert "127.0.0.1:1" in str(ei.value)
    assert sess.profile.username == "u"
