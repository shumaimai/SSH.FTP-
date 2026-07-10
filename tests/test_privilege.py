from hashi.permjournal import PermJournal
from hashi.privilege import PermManager
from tests.conftest import FakeSFTP, FakeSession


def test_refcount_single_grant_and_restore(tmp_path):
    sftp = FakeSFTP({"/f": 0o000}, owned=["/f"])
    sess = FakeSession(sftp)
    pm = PermManager(sess, journal=PermJournal(tmp_path / "j.json"),
                     conn_id="A@h:22")
    pm._acquire("/f", 0o444)
    pm._acquire("/f", 0o444)            # 2 回目は refcount のみ
    assert [c for c in sftp.chmods if c[0] == "/f"] == [("/f", 0o444)]
    assert pm.journal.has_pending("A@h:22")
    pm._release("/f")
    assert pm.journal.has_pending("A@h:22")   # まだ参照が残る
    pm._release("/f")
    assert sftp.chmods[-1] == ("/f", 0o000)   # 元へ復元
    assert not pm.journal.has_pending("A@h:22")


def test_already_sufficient_is_noop(tmp_path):
    sftp = FakeSFTP({"/f": 0o777}, owned=["/f"])
    sess = FakeSession(sftp)
    pm = PermManager(sess, journal=PermJournal(tmp_path / "j.json"),
                     conn_id="A@h:22")
    pm._acquire("/f", 0o444)   # 既に十分 → chmod もジャーナルも無し
    assert sftp.chmods == []
    assert not pm.journal.has_pending("A@h:22")
    pm._release("/f")
    assert sftp.chmods == []


def test_recover_only_dead_pid_and_sudo_retry(tmp_path):
    import os
    j = PermJournal(tmp_path / "j.json")
    # root 所有(=owned でない)2 件 + 自分所有 1 件、すべて死んだ pid
    j.record("A@h:22", "/srv/a/b/deep.txt", 0o000, 2_000_000_000)
    j.record("A@h:22", "/srv/a", 0o750, 2_000_000_000)
    j.record("A@h:22", "/home/me/mine.txt", 0o644, 2_000_000_000)
    # 生存 pid のエントリは触られないこと
    j.record("A@h:22", "/live", 0o600, os.getpid())

    sftp = FakeSFTP(
        {"/srv/a/b/deep.txt": 0o444, "/srv/a": 0o757,
         "/home/me/mine.txt": 0o666, "/live": 0o666},
        owned=["/home/me/mine.txt"],
    )
    sess = FakeSession(sftp)
    pm = PermManager(sess, journal=j, conn_id="A@h:22")

    restored, stuck = pm.recover_pending()
    assert (restored, stuck) == (1, 2)          # 自分所有のみ復元
    # 深いパスを先に試している
    order = [c[0].split()[-1] for c in sess.sudo_calls]
    assert order.index("/srv/a/b/deep.txt") < order.index("/srv/a")
    # 生存 pid の /live は残る
    assert any(e["path"] == "/live" for e in j.pending_for("A@h:22"))

    # sudo を入手して再実行 → 残りも復元
    sess.sudo_ok = True
    pm.sudo_pw = "testpass"
    restored2, stuck2 = pm.recover_pending()
    assert (restored2, stuck2) == (2, 0)
    remaining = {e["path"] for e in j.pending_for("A@h:22")}
    assert remaining == {"/live"}               # 生存 pid の分だけ残る
