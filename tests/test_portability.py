"""portability.py(接続情報の書き出し / 読み込み、Issue #42)のテスト。"""
import json

import pytest

from hashi.config import KnownHosts, Profile, ProfileStore
from hashi.portability import (
    Bundle,
    PortabilityError,
    export_bundle,
    load_bundle,
    merge_bundle,
)


class FakeCredentials:
    """CredentialStore 互換の最小フェイク。"""

    available = True

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, profile, kind):
        return self.data.get(f"{profile.id_str()}:{kind}")

    def set(self, profile, kind, secret):
        self.data[f"{profile.id_str()}:{kind}"] = secret
        return True


@pytest.fixture()
def env(tmp_path):
    store = ProfileStore(path=tmp_path / "profiles.json")
    kh = KnownHosts(path=tmp_path / "known_hosts.json")
    return store, kh, tmp_path


def _profile(host="203.0.113.10", user="deploy", **kw):
    return Profile(name=f"{user}@{host}", host=host, username=user, **kw)


def test_roundtrip_without_secrets(env):
    store, kh, tmp = env
    p = _profile(proxy_jump="ops@bastion")
    kh.remember(p.host, p.port, "ssh-ed25519", "SHA256:abc")
    path = tmp / "export.json"
    counts = export_bundle(path, [p], kh)
    assert counts == {"profiles": 1, "secrets": 0}

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["format"] == "hashi-export" and raw["version"] == 1
    assert "secrets" not in raw
    assert "203.0.113.10:22" in raw["known_hosts"]

    bundle = load_bundle(path)
    assert not bundle.has_encrypted_secrets
    merged = merge_bundle(bundle, store, kh)
    assert merged["added"] == 1 and merged["secrets"] == 0
    assert store.profiles[0].proxy_jump == "ops@bastion"


def test_secrets_are_encrypted_and_roundtrip(env):
    store, kh, tmp = env
    p = _profile()
    creds = FakeCredentials({f"{p.id_str()}:password": "pw1",
                             f"{p.id_str()}:sudo": "su1"})
    path = tmp / "export.json"
    counts = export_bundle(path, [p], kh, creds, passphrase="corr3ct")
    assert counts["secrets"] == 2

    text = path.read_text(encoding="utf-8")
    assert "pw1" not in text and "su1" not in text  # 平文は絶対に書かない

    bundle = load_bundle(path)
    assert bundle.has_encrypted_secrets
    bundle.decrypt_secrets("corr3ct")
    dst_creds = FakeCredentials()
    merged = merge_bundle(bundle, store, kh, dst_creds)
    assert merged["secrets"] == 2
    assert dst_creds.get(p, "password") == "pw1"
    assert dst_creds.get(p, "sudo") == "su1"


def test_wrong_passphrase_raises(env):
    store, kh, tmp = env
    p = _profile()
    creds = FakeCredentials({f"{p.id_str()}:password": "pw1"})
    path = tmp / "export.json"
    export_bundle(path, [p], kh, creds, passphrase="right")
    bundle = load_bundle(path)
    with pytest.raises(PortabilityError, match="パスフレーズ"):
        bundle.decrypt_secrets("wrong")


def test_no_passphrase_means_no_secrets_even_with_credentials(env):
    _store, kh, tmp = env
    p = _profile()
    creds = FakeCredentials({f"{p.id_str()}:password": "pw1"})
    path = tmp / "export.json"
    counts = export_bundle(path, [p], kh, creds, passphrase=None)
    assert counts["secrets"] == 0
    assert "pw1" not in path.read_text(encoding="utf-8")


def test_duplicate_skip_and_overwrite(env):
    store, kh, tmp = env
    store.profiles.append(_profile(initial_path="/old"))
    store.save()
    path = tmp / "export.json"
    export_bundle(path, [_profile(initial_path="/new")], kh)

    bundle = load_bundle(path)
    merged = merge_bundle(bundle, store, kh, overwrite=False)
    assert merged["skipped"] == 1 and store.profiles[0].initial_path == "/old"

    merged = merge_bundle(load_bundle(path), store, kh, overwrite=True)
    assert merged["updated"] == 1 and store.profiles[0].initial_path == "/new"
    assert len(store.profiles) == 1


def test_known_hosts_never_overwritten_on_import(env):
    """インポートで既存のホスト鍵記録を上書きしない(警告の黙殺防止)。"""
    store, kh, tmp = env
    kh.remember("203.0.113.10", 22, "ssh-ed25519", "SHA256:existing")
    bundle = Bundle(known_hosts={
        "203.0.113.10:22": {"key_type": "ssh-ed25519",
                            "fingerprint": "SHA256:attacker"},
        "198.51.100.1:22": {"key_type": "ssh-rsa",
                            "fingerprint": "SHA256:new"},
    })
    merged = merge_bundle(bundle, store, kh)
    assert merged["hosts_added"] == 1
    assert kh.check("203.0.113.10", 22, "ssh-ed25519",
                    "SHA256:existing")[0] == "match"
    assert kh.check("198.51.100.1", 22, "ssh-rsa", "SHA256:new")[0] == "match"


def test_load_rejects_foreign_and_future_files(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(PortabilityError, match="エクスポートファイルでは"):
        load_bundle(p)
    p.write_text('{"format": "hashi-export", "version": 999}',
                 encoding="utf-8")
    with pytest.raises(PortabilityError, match="新しいバージョン"):
        load_bundle(p)
    p.write_text("not json", encoding="utf-8")
    with pytest.raises(PortabilityError):
        load_bundle(p)


def test_secrets_not_applied_to_skipped_profiles(env):
    """スキップした既存プロファイルへは秘密情報を書き込まない。"""
    store, kh, tmp = env
    p = _profile()
    store.profiles.append(_profile())
    store.save()
    creds = FakeCredentials({f"{p.id_str()}:password": "pw1"})
    path = tmp / "export.json"
    export_bundle(path, [p], kh, creds, passphrase="pp")
    bundle = load_bundle(path)
    bundle.decrypt_secrets("pp")
    dst = FakeCredentials()
    merged = merge_bundle(bundle, store, kh, dst, overwrite=False)
    assert merged["skipped"] == 1 and merged["secrets"] == 0
    assert dst.data == {}
