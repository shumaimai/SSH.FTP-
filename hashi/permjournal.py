"""権限無視スイッチのジャーナル。

権限を緩める **前** に「元の権限」をディスクへ記録(fsync)しておく。
正常時は操作後に復元してエントリを消す。もしプロセスが強制終了されて
復元できなくても、次回接続時にジャーナルを読んで元の権限へ戻す。

各エントリに記録元プロセスの pid を持たせ、復元は「その pid がもう
生きていない(=クラッシュした過去のプロセス)」エントリだけを対象にする。
これにより、同じサーバーへ同時接続している別の生存セッションが今まさに
緩めている最中のファイルを、誤って戻してしまう事故を防ぐ。

ファイルは JSON。書き込みは fsync + アトミック置換で、途中クラッシュしても
壊れない(最悪、最後の1件が反映されないだけで、復元は冪等なので安全)。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

from .config import config_dir
from .jsonio import load_json, save_json_atomic

logger = logging.getLogger(__name__)

# 同一プロセス内の全 PermJournal インスタンスでファイル操作を直列化
_FILE_LOCK = threading.RLock()


def pid_alive(pid: int | None) -> bool:
    """pid のプロセスが生存しているか。判定不能時は安全側(生存)に倒す。"""
    if not pid:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes
            PROCESS_QUERY_LIMITED = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            logger.debug("pid 生存判定 (Windows) に失敗 (安全側=生存とみなす)",
                         exc_info=True)
            return True  # 判定できないなら復元を控える
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # 存在するが別ユーザー
    except OSError:
        return False
    return True


class PermJournal:
    def __init__(self, path=None):
        self.path = path or (config_dir() / "perm_journal.json")
        self._counter = 0

    def _load(self) -> dict:
        return load_json(
            self.path,
            dict,
            logger=logger,
            warning="権限ジャーナルを読み込めません（未復元の権限が残る可能性）: %s",
        )

    def _save(self, data: dict) -> None:
        save_json_atomic(
            self.path,
            data,
            fsync=True,
            temp_suffix=".journal.tmp",
        )

    def record(self, conn_id: str, path: str, orig_mode: int, pid: int) -> str:
        """権限を緩める前に呼ぶ。エントリ ID を返す(ディスクへ fsync 済み)。"""
        with _FILE_LOCK:
            data = self._load()
            eid = f"{pid}-{time.time_ns()}-{self._counter}"
            self._counter += 1
            data[eid] = {
                "conn": conn_id, "path": path, "orig": orig_mode,
                "pid": pid, "ts": time.time(),
            }
            self._save(data)
            return eid

    def clear(self, entry_id: str) -> None:
        """復元が済んだエントリを消す。"""
        with _FILE_LOCK:
            data = self._load()
            if entry_id in data:
                del data[entry_id]
                self._save(data)

    def has_pending(self, conn_id: str) -> bool:
        with _FILE_LOCK:
            data = self._load()
        return any(v.get("conn") == conn_id for v in data.values())

    def pending_for(self, conn_id: str) -> list[dict]:
        """この接続に属する未復元エントリの一覧。"""
        with _FILE_LOCK:
            data = self._load()
        out = []
        for k, v in data.items():
            if v.get("conn") == conn_id:
                out.append({"id": k, **v})
        return out
