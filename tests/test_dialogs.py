from hashi.config import AUTH_AGENT, AUTH_KEY, AUTH_PASSWORD, Profile
from hashi.dialogs import ConnectDialog, KeygenDialog


class FakeCredentials:
    def __init__(self):
        self.stored = {}
        self.cleared = []

    def set(self, profile, kind, secret):
        self.stored[(profile.id_str(), kind)] = secret
        return True

    def clear_profile(self, profile):
        self.cleared.append(profile.id_str())
        for key in list(self.stored):
            if key[0] == profile.id_str():
                del self.stored[key]


def _select_auth(dialog, auth_method):
    dialog.cb_auth.setCurrentIndex(dialog.cb_auth.findData(auth_method))


def test_password_auth_replaces_key_fields(qapp):
    dialog = ConnectDialog(profile=Profile(auth_method=AUTH_PASSWORD))

    assert dialog._key_row.isHidden()
    assert not dialog.ed_password.isHidden()
    assert dialog.ed_passphrase.isHidden()


def test_key_auth_shows_password_and_passphrase(qapp):
    dialog = ConnectDialog(profile=Profile(auth_method=AUTH_KEY))

    assert not dialog._key_row.isHidden()
    assert not dialog.ed_password.isHidden()
    assert not dialog.ed_passphrase.isHidden()
    assert dialog._password_label.text() == "ログインパスワード（任意）"


def test_agent_auth_hides_unused_secret_fields(qapp):
    dialog = ConnectDialog(profile=Profile(auth_method=AUTH_AGENT))

    assert dialog._key_row.isHidden()
    assert dialog.ed_password.isHidden()
    assert dialog.ed_passphrase.isHidden()


def test_entered_password_and_passphrase_are_saved(qapp):
    credentials = FakeCredentials()
    profile = Profile(
        host="example.com", username="user", auth_method=AUTH_KEY,
        save_secrets=True,
    )
    dialog = ConnectDialog(profile=profile, credentials=credentials)
    dialog.ed_password.setText("login-secret")
    dialog.ed_passphrase.setText("key-secret")

    dialog.apply_credentials(profile)

    assert credentials.stored[(profile.id_str(), "password")] == "login-secret"
    assert credentials.stored[(profile.id_str(), "passphrase")] == "key-secret"


def test_blank_fields_do_not_overwrite_saved_secrets(qapp):
    credentials = FakeCredentials()
    profile = Profile(
        host="example.com", username="user", auth_method=AUTH_PASSWORD,
        save_secrets=True,
    )
    credentials.stored[(profile.id_str(), "password")] = "existing"
    dialog = ConnectDialog(profile=profile, credentials=credentials)

    dialog.apply_credentials(profile)

    assert credentials.stored[(profile.id_str(), "password")] == "existing"


def test_disabling_secret_storage_clears_profile(qapp):
    credentials = FakeCredentials()
    profile = Profile(
        host="example.com", username="user", auth_method=AUTH_PASSWORD,
        save_secrets=False,
    )
    credentials.stored[(profile.id_str(), "password")] = "existing"
    dialog = ConnectDialog(profile=profile, credentials=credentials)

    dialog.apply_credentials(profile)

    assert credentials.stored == {}
    assert credentials.cleared == [profile.id_str()]


def test_switching_auth_method_updates_visible_rows(qapp):
    dialog = ConnectDialog(profile=Profile(auth_method=AUTH_KEY))

    _select_auth(dialog, AUTH_PASSWORD)
    assert dialog._key_row.isHidden()
    assert not dialog.ed_password.isHidden()
    assert dialog.ed_passphrase.isHidden()

    _select_auth(dialog, AUTH_AGENT)
    assert dialog._key_row.isHidden()
    assert dialog.ed_password.isHidden()
    assert dialog.ed_passphrase.isHidden()


def test_keygen_dialog_uses_result_settings_and_confirms_overwrite(qapp, tmp_path, monkeypatch):
    import hashi.dialogs as dialogs

    existing = tmp_path / "id_ed25519"
    existing.write_text("old")
    dialog = KeygenDialog()
    dialog.ed_path.setText(str(existing))
    accepted = []
    dialog.accept = lambda: accepted.append(True)

    monkeypatch.setattr(
        dialogs.QMessageBox,
        "question",
        lambda *args, **kwargs: dialogs.QMessageBox.No,
    )
    dialog._validate_accept()
    assert accepted == []
    assert callable(dialog.result)
    assert dialog.result_settings()["path"] == str(existing)

    monkeypatch.setattr(
        dialogs.QMessageBox,
        "question",
        lambda *args, **kwargs: dialogs.QMessageBox.Yes,
    )
    dialog._validate_accept()
    assert accepted == [True]
