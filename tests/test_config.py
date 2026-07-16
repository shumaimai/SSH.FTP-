def test_settings_roundtrip(tmp_config):
    from hashi.config import Settings
    s = Settings()
    assert s.get("sudo_autofill") in (True, False)
    s.set("terminal_font_size", 15)
    s2 = Settings()                       # 保存 → 読み直し
    assert s2.get("terminal_font_size") == 15


def test_profile_store_roundtrip(tmp_config):
    from hashi.config import Profile, ProfileStore
    st = ProfileStore()
    st.profiles.append(Profile(name="srv", host="h", username="u",
                               save_secrets=True))
    st.save()
    st2 = ProfileStore()
    assert any(p.host == "h" and p.save_secrets for p in st2.profiles)


def test_known_hosts_tofu(tmp_config):
    from hashi.config import KnownHosts
    kh = KnownHosts()
    assert kh.check("h", 22, "ssh-ed25519", "FP1")[0] == "new"
    kh.remember("h", 22, "ssh-ed25519", "FP1")
    assert kh.check("h", 22, "ssh-ed25519", "FP1")[0] == "match"
    status, old = kh.check("h", 22, "ssh-ed25519", "FP2")
    assert status == "mismatch" and old == "FP1"


def test_wrong_json_types_warn_and_use_defaults(tmp_path, caplog):
    from hashi.config import KnownHosts, ProfileStore, Settings

    profiles_path = tmp_path / "profiles.json"
    settings_path = tmp_path / "settings.json"
    known_hosts_path = tmp_path / "known_hosts.json"
    profiles_path.write_text("{}", encoding="utf-8")
    settings_path.write_text("[]", encoding="utf-8")
    known_hosts_path.write_text("0", encoding="utf-8")

    profiles = ProfileStore(profiles_path)
    settings = Settings(settings_path)
    known_hosts = KnownHosts(known_hosts_path)

    assert profiles.profiles == []
    assert settings.get("terminal_font_size") == Settings.DEFAULTS["terminal_font_size"]
    assert known_hosts.check("h", 22, "ssh-ed25519", "FP1")[0] == "new"
    assert "profiles.json を読み込めません" in caplog.text
    assert "settings.json を読み込めません" in caplog.text
    assert "known_hosts.json を読み込めません" in caplog.text


def test_keepalive_interval_roundtrip(tmp_config):
    from hashi.config import Settings
    s = Settings()
    assert s.get("keepalive_interval") == 30
    s.set("keepalive_interval", 0)
    s2 = Settings()
    assert s2.get("keepalive_interval") == 0


def test_auto_reconnect_roundtrip(tmp_config):
    from hashi.config import Settings
    s = Settings()
    assert s.get("auto_reconnect") is True
    assert s.get("auto_reconnect_max") == 5
    s.set("auto_reconnect", False)
    s.set("auto_reconnect_max", 3)
    s2 = Settings()
    assert s2.get("auto_reconnect") is False
    assert s2.get("auto_reconnect_max") == 3
