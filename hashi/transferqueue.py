"""転送キューの追跡と一覧 UI。

SftpWorker(xfer)のジョブキューは queue.Queue のままで、その状態を
GUI 側で写し取る「台帳」(TransferQueue)と、それを表示するパネル
(TransferQueuePanel)を提供する。

- 台帳は GUI スレッドでのみ更新する(ワーカーからは Signal 経由)。
- 待機中ジョブのキャンセルは「取り消し済み ID 集合」方式
  (ワーカーが取り出した時点で読み飛ばす)。
- 失敗 / キャンセルしたアップロード・ダウンロードは、部分ファイルの
  サイズ比較によるレジューム(途中再開)で再実行できる。
"""
from __future__ import annotations

import itertools

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ジョブ状態
WAITING = "waiting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

STATE_LABELS = {
    WAITING: "待機中",
    RUNNING: "実行中",
    DONE: "完了",
    FAILED: "失敗",
    CANCELLED: "キャンセル",
}

# レジューム再実行できる種別
RESUMABLE_KINDS = ("upload", "download")

_id_counter = itertools.count(1)


class TransferJob:
    """キュー内の 1 ジョブの状態。GUI スレッドでのみ触る。"""

    def __init__(self, kind: str, label: str, payload: dict):
        self.id = next(_id_counter)
        self.kind = kind
        self.label = label
        self.payload = payload      # 再実行(レジューム)用に元のジョブ内容を保持
        self.state = WAITING
        self.error = ""
        self.done = 0
        self.total = 0

    @property
    def resumable(self) -> bool:
        return self.kind in RESUMABLE_KINDS and self.state in (FAILED, CANCELLED)


class TransferQueue(QObject):
    """転送ジョブの台帳。状態変化を changed で通知する。"""

    changed = Signal(object)  # TransferJob

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs: dict[int, TransferJob] = {}
        self._order: list[int] = []

    def add(self, kind: str, label: str, payload: dict) -> TransferJob:
        job = TransferJob(kind, label, payload)
        self._jobs[job.id] = job
        self._order.append(job.id)
        self.changed.emit(job)
        return job

    def get(self, job_id) -> TransferJob | None:
        return self._jobs.get(job_id)

    def jobs(self) -> list[TransferJob]:
        return [self._jobs[i] for i in self._order]

    def _set_state(self, job_id, state: str, error: str = ""):
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.state = state
        job.error = error
        self.changed.emit(job)

    def mark_running(self, job_id):
        self._set_state(job_id, RUNNING)

    def mark_done(self, job_id):
        self._set_state(job_id, DONE)

    def mark_failed(self, job_id, error: str = ""):
        self._set_state(job_id, FAILED, error)

    def mark_cancelled(self, job_id):
        job = self._jobs.get(job_id)
        # 完了済みを後からキャンセル扱いにしない
        if job is None or job.state in (DONE, FAILED):
            return
        self._set_state(job_id, CANCELLED)

    def mark_waiting(self, job_id):
        """レジューム再投入時に待機中へ戻す。"""
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.done = 0
        job.total = 0
        self._set_state(job_id, WAITING)

    def update_progress(self, job_id, done: int, total: int):
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.done = done
        job.total = total
        self.changed.emit(job)

    def running_job(self) -> TransferJob | None:
        for jid in self._order:
            if self._jobs[jid].state == RUNNING:
                return self._jobs[jid]
        return None

    def clear_finished(self):
        removed = [i for i in self._order
                   if self._jobs[i].state in (DONE, FAILED, CANCELLED)]
        for i in removed:
            job = self._jobs.pop(i)
            self._order.remove(i)
            self.changed.emit(job)


class TransferQueuePanel(QWidget):
    """転送キューの一覧パネル。キャンセル / レジューム / 履歴クリア。"""

    cancel_requested = Signal(int)   # job id
    resume_requested = Signal(int)   # job id

    def __init__(self, tq: TransferQueue, parent=None):
        super().__init__(parent)
        self.tq = tq
        self._items: dict[int, QTreeWidgetItem] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["状態", "種類", "内容", "進捗"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tree.setColumnWidth(0, 80)
        self.tree.setColumnWidth(1, 100)
        root.addWidget(self.tree, 1)

        btns = QHBoxLayout()
        self.b_cancel = QPushButton("キャンセル")
        self.b_cancel.setToolTip("選択したジョブを取り消す (実行中なら中断)")
        self.b_cancel.clicked.connect(self._cancel_clicked)
        self.b_resume = QPushButton("再開")
        self.b_resume.setToolTip(
            "失敗 / キャンセルした転送を途中から再開する\n"
            "(部分ファイルのサイズを比較し、続きだけ転送します)")
        self.b_resume.clicked.connect(self._resume_clicked)
        self.b_clear = QPushButton("完了分をクリア")
        self.b_clear.clicked.connect(self.tq.clear_finished)
        for b in (self.b_cancel, self.b_resume, self.b_clear):
            btns.addWidget(b)
        btns.addStretch(1)
        root.addLayout(btns)

        self.tq.changed.connect(self._on_changed)
        for job in self.tq.jobs():
            self._on_changed(job)

    _KIND_LABELS = {
        "upload": "アップロード",
        "download": "ダウンロード",
        "delete": "削除",
    }

    @staticmethod
    def _progress_text(job: TransferJob) -> str:
        if job.state != RUNNING or job.total <= 0:
            return ""
        pct = min(100, int(job.done * 100 / job.total))
        return f"{pct}%"

    def _on_changed(self, job: TransferJob):
        if self.tq.get(job.id) is None:
            item = self._items.pop(job.id, None)
            if item is not None:
                idx = self.tree.indexOfTopLevelItem(item)
                if idx >= 0:
                    self.tree.takeTopLevelItem(idx)
            return
        item = self._items.get(job.id)
        if item is None:
            item = QTreeWidgetItem()
            item.setData(0, Qt.UserRole, job.id)
            self.tree.addTopLevelItem(item)
            self._items[job.id] = item
        item.setText(0, STATE_LABELS.get(job.state, job.state))
        item.setText(1, self._KIND_LABELS.get(job.kind, job.kind))
        item.setText(2, job.label if not job.error else f"{job.label} — {job.error}")
        item.setText(3, self._progress_text(job))

    def _selected_id(self):
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, Qt.UserRole)

    def _cancel_clicked(self):
        jid = self._selected_id()
        if jid is None:
            return
        job = self.tq.get(jid)
        if job is not None and job.state in (WAITING, RUNNING):
            self.cancel_requested.emit(jid)

    def _resume_clicked(self):
        jid = self._selected_id()
        if jid is None:
            return
        job = self.tq.get(jid)
        if job is not None and job.resumable:
            self.resume_requested.emit(jid)
