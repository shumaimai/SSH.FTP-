"""転送キュー台帳とレジューム転送のテスト。"""
import io

import pytest

# ---- 台帳 (TransferQueue) ------------------------------------------------

def test_queue_state_transitions(qapp):
    from hashi.transferqueue import (
        CANCELLED,
        DONE,
        FAILED,
        RUNNING,
        WAITING,
        TransferQueue,
    )
    tq = TransferQueue()
    events = []
    tq.changed.connect(lambda j: events.append((j.id, j.state)))

    j1 = tq.add("upload", "a.txt", {"files": []})
    j2 = tq.add("download", "b.txt", {"items": []})
    assert j1.state == WAITING and j2.state == WAITING

    tq.mark_running(j1.id)
    assert tq.get(j1.id).state == RUNNING
    assert tq.running_job() is tq.get(j1.id)
    tq.mark_done(j1.id)
    assert tq.get(j1.id).state == DONE
    assert tq.running_job() is None

    tq.mark_failed(j2.id, "boom")
    assert tq.get(j2.id).state == FAILED
    assert tq.get(j2.id).error == "boom"
    # 失敗済みを後からキャンセル扱いにしない
    tq.mark_cancelled(j2.id)
    assert tq.get(j2.id).state == FAILED

    j3 = tq.add("download", "c.txt", {"items": []})
    tq.mark_cancelled(j3.id)
    assert tq.get(j3.id).state == CANCELLED
    assert (j1.id, RUNNING) in events


def test_queue_resumable_and_clear(qapp):
    from hashi.transferqueue import WAITING, TransferQueue
    tq = TransferQueue()
    up = tq.add("upload", "a", {})
    dl = tq.add("download", "b", {})
    rm = tq.add("delete", "c", {})
    for j in (up, dl, rm):
        tq.mark_failed(j.id)
    assert up.resumable and dl.resumable
    assert not rm.resumable  # 削除はレジューム不可

    # レジューム再投入で待機中に戻り、進捗はリセット
    up.done, up.total = 5, 10
    tq.mark_waiting(up.id)
    assert up.state == WAITING and up.done == 0 and up.total == 0

    running = tq.add("upload", "d", {})
    tq.mark_running(running.id)
    tq.clear_finished()
    ids = [j.id for j in tq.jobs()]
    assert dl.id not in ids and rm.id not in ids
    assert up.id in ids and running.id in ids  # 待機/実行中は残る


def test_queue_progress(qapp):
    from hashi.transferqueue import TransferQueue
    tq = TransferQueue()
    j = tq.add("download", "a", {})
    tq.mark_running(j.id)
    tq.update_progress(j.id, 30, 100)
    assert j.done == 30 and j.total == 100
    tq.update_progress(999, 1, 2)  # 不明 ID は無視


def test_panel_lists_jobs(qapp):
    from hashi.transferqueue import TransferQueue, TransferQueuePanel
    tq = TransferQueue()
    panel = TransferQueuePanel(tq)
    j = tq.add("upload", "a.txt", {})
    assert panel.tree.topLevelItemCount() == 1
    tq.mark_failed(j.id, "err")
    item = panel.tree.topLevelItem(0)
    assert "失敗" in item.text(0)
    assert "err" in item.text(2)
    tq.clear_finished()
    assert panel.tree.topLevelItemCount() == 0


# ---- ワーカーの個別キャンセル / レジューム転送 -----------------------------

class _FakeRemoteFile(io.BytesIO):
    """seek / read / write / close を備えた最小のリモートファイル。"""

    def __init__(self, store, path, mode):
        data = store.get(path, b"")
        # 追記モードは書いた分だけ貯めて close で結合する
        super().__init__(data if "r" in mode else b"")
        self._append_base = data if "a" in mode else b""
        self.store = store
        self.path = path
        self.mode = mode

    def __exit__(self, *a):
        self.close()
        return False

    def __enter__(self):
        return self

    def close(self):
        if "a" in self.mode:
            self.store[self.path] = self._append_base + self.getvalue()
        elif "w" in self.mode:
            self.store[self.path] = self.getvalue()
        super().close()


class _FakeSftpFiles:
    """ファイル内容を dict で持つフェイク SFTP。"""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.full_gets = []
        self.full_puts = []

    def stat(self, path):
        if path not in self.files:
            raise IOError(2, "No such file")

        class A:
            st_size = len(self.files[path])
        return A()

    def open(self, path, mode="rb"):
        return _FakeRemoteFile(self.files, path, mode)

    def get(self, remote, local, callback=None):
        self.full_gets.append(remote)
        with open(local, "wb") as f:
            f.write(self.files[remote])

    def put(self, local, remote, callback=None):
        self.full_puts.append(remote)
        with open(local, "rb") as f:
            self.files[remote] = f.read()


@pytest.fixture
def worker(qapp):
    from hashi.filebrowser import SftpWorker

    sftp = _FakeSftpFiles()

    class _S:
        @staticmethod
        def open_sftp():
            return sftp

    w = SftpWorker(_S(), "xfer")  # スレッドは起動せず直接メソッドを呼ぶ
    w.sftp = sftp
    return w


def test_download_resume_appends_from_offset(worker, tmp_path):
    data = b"0123456789" * 100
    worker.sftp.files["/r/f.bin"] = data
    local = tmp_path / "f.bin"
    local.write_bytes(data[:300])  # 部分ファイルあり

    progress = []
    worker._download_file("/r/f.bin", str(local), len(data),
                          lambda d, t: progress.append((d, t)), resume=True)
    assert local.read_bytes() == data
    assert worker.sftp.full_gets == []  # 全量取得は走らない
    assert progress and progress[0][0] > 300 and progress[-1] == (len(data), len(data))


def test_download_without_partial_transfers_full(worker, tmp_path):
    data = b"abc" * 50
    worker.sftp.files["/r/g.bin"] = data
    local = tmp_path / "g.bin"
    worker._download_file("/r/g.bin", str(local), len(data), None, resume=True)
    assert local.read_bytes() == data  # 部分ファイルなし → 先頭から全体を取得


def test_upload_resume_appends_remote(worker, tmp_path):
    data = b"x" * 500 + b"y" * 500
    local = tmp_path / "u.bin"
    local.write_bytes(data)
    worker.sftp.files["/r/u.bin"] = data[:400]  # リモートに部分ファイル

    worker._upload_file(str(local), "/r/u.bin", None, resume=True)
    assert worker.sftp.files["/r/u.bin"] == data
    assert worker.sftp.full_puts == []


def test_upload_resume_remote_larger_overwrites(worker, tmp_path):
    """リモートが既にローカル以上のサイズなら通常の上書きにする。"""
    local = tmp_path / "v.bin"
    local.write_bytes(b"a" * 100)
    worker.sftp.files["/r/v.bin"] = b"b" * 200
    worker._upload_file(str(local), "/r/v.bin", None, resume=True)
    assert worker.sftp.files["/r/v.bin"] == b"a" * 100


def test_download_cancel_stops_midway(worker, tmp_path):
    """実行中の転送はチャンクごとにキャンセル判定され、途中で中断する。"""
    from hashi.filebrowser import STREAM_CHUNK, _Cancelled

    data = b"z" * (STREAM_CHUNK * 3)
    worker.sftp.files["/r/big.bin"] = data
    local = tmp_path / "big.bin"

    def cb(done, _total):
        # 最初のチャンク書き込み後にキャンセル要求が来た状況を再現
        worker._cancel = True

    with pytest.raises(_Cancelled):
        worker._download_file("/r/big.bin", str(local), len(data), cb)
    # 全量ではなく途中まで(1 チャンク)しか書かれていない
    assert 0 < local.stat().st_size < len(data)


def test_upload_cancel_stops_midway(worker, tmp_path):
    from hashi.filebrowser import STREAM_CHUNK, _Cancelled

    data = b"y" * (STREAM_CHUNK * 3)
    local = tmp_path / "u.bin"
    local.write_bytes(data)

    def cb(done, _total):
        worker._cancel = True

    with pytest.raises(_Cancelled):
        worker._upload_file(str(local), "/r/u.bin", cb)
    assert 0 < len(worker.sftp.files.get("/r/u.bin", b"")) < len(data)


def test_cancel_job_skips_waiting(worker):
    """待機中ジョブは取り出し時に読み飛ばされ cancelled が通知される。"""
    worker.cancel_job(42)
    assert 42 in worker._cancelled_ids
    finished = []
    worker.job_finished.connect(lambda i, o, m: finished.append((i, o)))
    worker.enqueue({"kind": "list", "path": "/", "id": 42})
    worker.enqueue(None)
    worker.run()
    assert (42, "cancelled") in finished


def test_cancel_job_running_sets_cancel_flag(worker):
    worker._current_id = 7
    worker.cancel_job(7)
    assert worker._cancel is True
