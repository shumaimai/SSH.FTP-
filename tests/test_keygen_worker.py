from hashi.mainwindow import KeygenWorker


def test_keygen_worker_reports_saved_key_when_registration_fails(qapp, monkeypatch, tmp_path):
    import hashi.mainwindow as mainwindow

    saved = []

    class Generated:
        public_line = "ssh-ed25519 AAAA"

        def write_private_key(self, path, passphrase):
            saved.append((path, passphrase))

    monkeypatch.setattr(mainwindow, "generate_key", lambda *args: Generated())

    def fail_registration(_session, _public_line):
        raise RuntimeError("登録エラー")

    monkeypatch.setattr(mainwindow, "register_public_key", fail_registration)
    settings = {
        "key_type": "ed25519",
        "bits": None,
        "passphrase": "秘密",
        "comment": "",
        "path": str(tmp_path / "id_ed25519"),
        "register": True,
    }
    worker = KeygenWorker(settings, session=object())
    failures = []
    worker.fail.connect(failures.append)

    worker.run()

    assert saved == [(settings["path"], "秘密")]
    assert failures == [
        "秘密鍵の保存は完了しました。公開鍵の登録のみ失敗しました。\n登録エラー"
    ]
