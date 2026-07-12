"""アカウント同期(Issue #44)のテスト。

Google には触らず、フェイク backend で E2E 暗号と push/pull/統合を検証する。
"""
import pytest

from hashi import cloudsync
from hashi.cloudsync import (
    CloudSyncError,
    decrypt_blob,
    encrypt_blob,
    pull,
    pull_and_merge,
    push,
)
from hashi.config import KnownHosts, Profile, ProfileStore


class FakeBackend:
    """メモリ上の 1 スロット backend。"""

    def __init__(self):
        self.blob = None

    def get(self):
        return self.blob

    def put(self, data):
        self.blob = data


class FakeCredentials:
    available = True

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, profile, kind):
        return self.data.get(f"{profile.id_str()}:{kind}")

    def set(self, profile, kind, secret):
        self.data[f"{profile.id_str()}:{kind}"] = secret
        return True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    store = ProfileStore(path=tmp_path / "profiles.json")
    kh = KnownHosts(path=tmp_path / "known_hosts.json")
    return store, kh, tmp_path


def _profile(host="203.0.113.10", user="deploy", **kw):
    return Profile(name=f"{user}@{host}", host=host, username=user, **kw)


# ---- E2E 暗号 ---------------------------------------------------------------

def test_encrypt_decrypt_roundtrip():
    blob = encrypt_blob(b"secret payload \xf0\x9f\x94\x91", "master-pw")
    assert b"secret payload" not in blob        # 平文は封筒に出ない
    assert decrypt_blob(blob, "master-pw") == b"secret payload \xf0\x9f\x94\x91"


def test_wrong_master_passphrase_fails():
    blob = encrypt_blob(b"data", "right")
    with pytest.raises(CloudSyncError, match="マスターパスフレーズ"):
        decrypt_blob(blob, "wrong")


def test_empty_master_passphrase_rejected():
    with pytest.raises(CloudSyncError, match="空にはできません"):
        encrypt_blob(b"x", "")


def test_decrypt_rejects_foreign_blob():
    with pytest.raises(CloudSyncError, match="同期データ"):
        decrypt_blob(b'{"hello":1}', "pw")
    with pytest.raises(CloudSyncError):
        decrypt_blob(b"not json", "pw")


# ---- push / pull ------------------------------------------------------------

def test_push_pull_roundtrip_without_secrets(env):
    store, kh, _ = env
    p = _profile(proxy_jump="ops@bastion")
    kh.remember(p.host, p.port, "ssh-ed25519", "SHA256:abc")
    backend = FakeBackend()

    res = push(backend, [p], kh, "master")
    assert res["profiles"] == 1 and res["secrets"] == 0
    assert backend.blob is not None

    bundle = pull(backend, "master")
    assert bundle.profiles[0].proxy_jump == "ops@bastion"
    assert "203.0.113.10:22" in bundle.known_hosts
    assert not bundle.has_encrypted_secrets


def test_push_includes_encrypted_secrets(env):
    store, kh, _ = env
    p = _profile()
    creds = FakeCredentials({f"{p.id_str()}:password": "pw1"})
    backend = FakeBackend()

    res = push(backend, [p], kh, "master", creds, secrets_passphrase="sekret")
    assert res["secrets"] == 1
    assert b"pw1" not in backend.blob                # E2E 封筒に平文なし

    bundle = pull(backend, "master")
    assert bundle.has_encrypted_secrets
    bundle.decrypt_secrets("sekret")
    assert bundle.secrets[p.id_str()]["password"] == "pw1"


def test_pull_empty_backend_returns_none(env):
    assert pull(FakeBackend(), "master") is None


def test_pull_and_merge_last_write_wins_with_backup(env):
    store, kh, tmp = env
    # 手元は古い initial_path
    store.profiles.append(_profile(initial_path="/old"))
    store.save()
    # クラウドには新しい initial_path
    other_kh = KnownHosts(path=tmp / "other_kh.json")
    backend = FakeBackend()
    push(backend, [_profile(initial_path="/new")], other_kh, "master")

    counts = pull_and_merge(backend, "master", store, kh, overwrite=True)
    assert counts["updated"] == 1
    assert store.profiles[0].initial_path == "/new"     # last-write-wins
    # 取り込み前のバックアップが作られている
    assert counts["backup"] and counts["backup"].endswith(".json")
    from pathlib import Path
    assert Path(counts["backup"]).exists()


def test_pull_and_merge_empty(env):
    store, kh, _ = env
    counts = pull_and_merge(FakeBackend(), "master", store, kh)
    assert counts["empty"] is True
    assert counts["backup"] is None


def test_pull_and_merge_restores_secrets(env):
    store, kh, tmp = env
    p = _profile()
    src_creds = FakeCredentials({f"{p.id_str()}:password": "pw1"})
    backend = FakeBackend()
    push(backend, [p], KnownHosts(path=tmp / "s_kh.json"),
         "master", src_creds, secrets_passphrase="sp")

    dst_creds = FakeCredentials()
    counts = pull_and_merge(backend, "master", store, kh, dst_creds,
                            secrets_passphrase="sp", overwrite=True)
    assert counts["secrets"] == 1
    assert dst_creds.get(p, "password") == "pw1"


def test_backend_failure_is_wrapped(env):
    class Broken:
        def get(self):
            raise RuntimeError("network down")

        def put(self, data):
            raise RuntimeError("network down")

    store, kh, _ = env
    with pytest.raises(CloudSyncError, match="アップロード"):
        push(Broken(), [_profile()], kh, "master")
    with pytest.raises(CloudSyncError, match="ダウンロード"):
        pull(Broken(), "master")


def test_default_client_config_from_env(monkeypatch):
    monkeypatch.delenv("HASHI_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("HASHI_GOOGLE_CLIENT_SECRET", raising=False)
    assert cloudsync._default_client_config() is None
    monkeypatch.setenv("HASHI_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("HASHI_GOOGLE_CLIENT_SECRET", "sec")
    cfg = cloudsync._default_client_config()
    assert cfg["installed"]["client_id"] == "cid"
