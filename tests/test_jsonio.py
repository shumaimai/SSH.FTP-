import json
import logging

from hashi.jsonio import load_json, save_json_atomic


def test_load_fallback_and_atomic_save(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    assert load_json(path, dict) == {}

    save_json_atomic(path, {"表示名": "自宅サーバー"}, ensure_ascii=False, indent=2)

    assert json.loads(path.read_text(encoding="utf-8")) == {"表示名": "自宅サーバー"}
    assert not path.with_suffix(".tmp").exists()


def test_load_warns_and_falls_back_for_wrong_type(tmp_path, caplog):
    path = tmp_path / "settings.json"
    path.write_text("[]", encoding="utf-8")
    logger = logging.getLogger("test_jsonio")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert load_json(
            path,
            dict,
            logger=logger,
            warning="設定を読み込めません: %s",
        ) == {}

    assert "設定を読み込めません" in caplog.text
    assert "expected dict, got list" in caplog.text


def test_fsync_option(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("hashi.jsonio.os.fsync", calls.append)

    save_json_atomic(tmp_path / "journal.json", {"entry": {}}, fsync=True)

    assert len(calls) == 1
