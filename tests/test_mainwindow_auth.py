"""認証失敗時に、使った保存済み認証情報が削除されることの回帰テスト。

以前は削除するつもりのコードが `pass` になっていて、誤ったパスワードが
保存されたままだと次回以降も自動で失敗し続けていた。
"""
from hashi.config import AUTH_PASSWORD, KnownHosts, Profile


class FakeCredentials:
    """CredentialStore の最小フェイク。保存・削除を記録する。"""

    def __init__(self, stored):
        self.stored = dict(stored)     # {(id, kind): secret}
        self.deleted = []
        self.available = True

    def get(self, profile, kind):
        return self.stored.get((profile.id_str(), kind))

    def set(self, profile, kind, secret):
        self.stored[(profile.id_str(), kind)] = secret
        return True

    def delete(self, profile, kind):
        self.deleted.append(kind)
        self.stored.pop((profile.id_str(), kind), None)


class FakeAuthSession:
    """connect() で保存済みパスワードを 1 回引き出してから認証失敗を投げる。"""

    def __init__(self, profile, known_hosts=None):
        self.profile = profile

    def connect(self, ui):
        ui.get_secret(f"{self.profile.username}@{self.profile.host} のパスワードを入力")
        raise Exception("Authentication failed.")


def test_auth_failure_clears_saved_credentials(qapp, monkeypatch):
    import hashi.mainwindow as mw
    monkeypatch.setattr(mw, "SshSession", FakeAuthSession)

    profile = Profile(host="h", port=22, username="u", auth_method=AUTH_PASSWORD)
    creds = FakeCredentials({(profile.id_str(), "password"): "wrong"})
    worker = mw.ConnectWorker(profile, KnownHosts(), creds)

    failures = []
    worker.fail.connect(failures.append)
    worker.run()

    assert "password" in creds.deleted
    assert (profile.id_str(), "password") not in creds.stored
    assert failures and failures[0]  # 空文字(キャンセル)ではなくエラー文字列


def test_auth_failure_keeps_non_auth_errors(qapp, monkeypatch):
    """認証以外の失敗(ネットワーク等)では保存済みを消さない。"""
    import hashi.mainwindow as mw

    class NetworkFailSession:
        def __init__(self, profile, known_hosts=None):
            self.profile = profile

        def connect(self, ui):
            ui.get_secret("パスワードを入力")
            raise Exception("host に接続できません")

    monkeypatch.setattr(mw, "SshSession", NetworkFailSession)

    profile = Profile(host="h", port=22, username="u", auth_method=AUTH_PASSWORD)
    creds = FakeCredentials({(profile.id_str(), "password"): "secret"})
    worker = mw.ConnectWorker(profile, KnownHosts(), creds)
    worker.fail.connect(lambda _m: None)
    worker.run()

    assert creds.deleted == []
    assert (profile.id_str(), "password") in creds.stored
