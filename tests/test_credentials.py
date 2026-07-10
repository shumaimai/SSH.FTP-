from hashi.config import Profile


def test_fernet_file_roundtrip(tmp_config):
    from hashi.credentials import _FernetFile
    f = _FernetFile()
    f.set("k1", "secret-value")
    assert f.get("k1") == "secret-value"
    f.delete("k1")
    assert f.get("k1") is None


def test_credential_store_roundtrip(tmp_config, monkeypatch):
    # keyring を強制的に無効化してファイルバックエンドを使わせる
    import hashi.credentials as creds

    class _NoKeyring:
        def _init_backend(self_inner):
            self_inner._keyring = None
            self_inner._file = creds._FernetFile()
            self_inner.backend_name = "encrypted-file"

    monkeypatch.setattr(creds.CredentialStore, "_init_backend",
                        _NoKeyring._init_backend)
    store = creds.CredentialStore()
    assert store.available
    assert store.is_secure() is False

    p = Profile(host="h", port=22, username="u")
    store.set(p, "password", "pw1")
    store.set(p, "sudo", "sudopw")
    assert store.get(p, "password") == "pw1"
    assert store.get(p, "sudo") == "sudopw"
    store.delete(p, "password")
    assert store.get(p, "password") is None
    store.clear_profile(p)
    assert store.get(p, "sudo") is None
