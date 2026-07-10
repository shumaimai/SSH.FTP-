import json

from hashi.jsonio import load_json, save_json_atomic


def test_load_fallback_and_atomic_save(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    assert load_json(path, dict) == {}

    save_json_atomic(path, {"表示名": "自宅サーバー"}, ensure_ascii=False, indent=2)

    assert json.loads(path.read_text(encoding="utf-8")) == {"表示名": "自宅サーバー"}
    assert not path.with_suffix(".tmp").exists()


def test_fsync_option(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("hashi.jsonio.os.fsync", calls.append)

    save_json_atomic(tmp_path / "journal.json", {"entry": {}}, fsync=True)

    assert len(calls) == 1
