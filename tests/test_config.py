def test_settings_roundtrip(tmp_config):
    from hashi.config import Settings
    s = Settings()
    assert s.get("sudo_autofill") in (True, False)
    s.set("terminal_font_size", 15)
    s2 = Settings()                       # 保存 → 読み直し
    assert s2.get("terminal_font_size") == 15


def test_profile_store_roundtrip(tmp_config):
    from hashi.config import ProfileStore, Profile
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
