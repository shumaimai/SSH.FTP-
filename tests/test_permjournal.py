import json
import os

from hashi.permjournal import PermJournal, pid_alive


def test_record_clear_pending(tmp_path):
    j = PermJournal(tmp_path / "j.json")
    assert j.pending_for("A@h:22") == []
    e1 = j.record("A@h:22", "/srv/a", 0o000, 111)
    j.record("A@h:22", "/srv/b", 0o755, 222)
    j.record("B@h:22", "/other", 0o600, 111)
    assert j.has_pending("A@h:22")
    assert len(j.pending_for("A@h:22")) == 2
    assert len(j.pending_for("B@h:22")) == 1
    j.clear(e1)
    paths = {e["path"] for e in j.pending_for("A@h:22")}
    assert paths == {"/srv/b"}


def test_pid_alive():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(2_000_000_000) is False
    assert pid_alive(None) is False


def test_survives_reload(tmp_path):
    p = tmp_path / "j.json"
    j = PermJournal(p)
    j.record("A@h:22", "/x", 0o644, 111)
    # 別インスタンスで読み直しても残っている(永続化)
    j2 = PermJournal(p)
    assert j2.has_pending("A@h:22")


def test_corrupt_journal_recovers_on_next_record(tmp_path):
    p = tmp_path / "j.json"
    p.write_text("{broken", encoding="utf-8")

    PermJournal(p).record("A@h:22", "/x", 0o644, 111)

    entries = json.loads(p.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in entries.values()] == ["/x"]
