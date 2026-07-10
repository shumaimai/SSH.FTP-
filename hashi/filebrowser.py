"""SFTP ファイルブラウザ。

ローカルのエクスプローラに近い操作感を目指す:
- ダブルクリックでフォルダ移動 / ファイルを開く(一時DLして関連付けアプリで開く)
- エクスプローラからのドラッグ&ドロップでアップロード
- F2 リネーム / Del 削除 / F5 更新 / Backspace 上へ
- 削除・上書きは必ず 2 段階確認 (一覧確認 → 確認語入力)

スレッド構成:
- nav ワーカー  : 一覧取得・mkdir・rename・アップロード前チェック
- xfer ワーカー : アップロード/ダウンロード/削除/一時DL (進捗つき)
それぞれ独立した SFTP チャネルを持つため、転送中もブラウズできる。
"""
from __future__ import annotations

import logging
import ntpath
import os
import posixpath
import queue
import stat as statmod
import tempfile
import time
from datetime import datetime
from html import escape

from PySide6.QtCore import QFileSystemWatcher, QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dialogs import DoubleCheckDialog
from .editor import EditorWindow
from .permjournal import PermJournal
from .privilege import OverrideError, PermManager

logger = logging.getLogger(__name__)

OPEN_SIZE_WARN = 50 * 1024 * 1024  # ダブルクリックで開く際の警告サイズ
EDIT_SIZE_LIMIT = 8 * 1024 * 1024  # 内蔵エディタで開く上限
TEXT_EXTS = {
    "txt", "md", "markdown", "log", "conf", "cfg", "ini", "toml", "yaml", "yml",
    "json", "xml", "html", "htm", "css", "js", "jsx", "ts", "tsx", "py", "pyw",
    "sh", "bash", "zsh", "c", "h", "cpp", "cc", "hpp", "java", "cs", "go", "rs",
    "rb", "php", "pl", "sql", "csv", "tsv", "env", "service", "rules", "list",
    "gitignore", "dockerfile", "makefile", "properties",
}


def _safe_local_child(root: str, parent: str, name: str) -> str:
    if not isinstance(name, str) or not name or name in (".", ".."):
        raise ValueError("不正なリモートファイル名です")
    drive, _ = ntpath.splitdrive(name)
    if drive or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"保存できないリモートファイル名です: {name!r}")

    root_abs = os.path.abspath(root)
    parent_abs = os.path.abspath(parent)
    try:
        if os.path.commonpath((root_abs, parent_abs)) != root_abs:
            raise ValueError("保存先フォルダの外には書き込めません")
    except ValueError as e:
        raise ValueError("保存先フォルダの外には書き込めません") from e

    root_real = os.path.realpath(root_abs)
    parent_real = os.path.realpath(parent_abs)
    try:
        if os.path.commonpath((root_real, parent_real)) != root_real:
            raise ValueError("保存先内のシンボリックリンクが外部を指しています")
    except ValueError as e:
        raise ValueError("保存先内のシンボリックリンクが外部を指しています") from e

    candidate = os.path.join(parent_abs, name)
    if os.path.lexists(candidate) and os.path.islink(candidate):
        raise ValueError(f"シンボリックリンクには上書きできません: {name}")
    return candidate


class _Cancelled(Exception):
    pass


def human_size(n) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def fmt_mtime(ts) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        logger.debug("mtime の整形に失敗: %r", ts, exc_info=True)
        return ""


def expand_local(paths: list[str], remote_dir: str):
    """ローカルのファイル/フォルダ群をアップロード計画に展開する。

    returns (files: [(local, remote)], dirs: [作成すべきリモートdir])
    """
    files: list[tuple[str, str]] = []
    dirs: list[str] = []
    for p in paths:
        p = os.path.normpath(p)
        base = os.path.basename(p.rstrip("\\/")) or p
        if os.path.isdir(p):
            for root, _subdirs, fnames in os.walk(p):
                rel = os.path.relpath(root, p)
                rel_posix = "" if rel == "." else rel.replace(os.sep, "/")
                rdir = posixpath.join(remote_dir, base, rel_posix) if rel_posix \
                    else posixpath.join(remote_dir, base)
                dirs.append(rdir)
                for f in fnames:
                    files.append((os.path.join(root, f), posixpath.join(rdir, f)))
        elif os.path.isfile(p):
            files.append((p, posixpath.join(remote_dir, base)))
    return files, dirs


IDLE_PROGRESS = {"label": "", "done": 0, "total": 0}


class ExternalFileMonitor(QObject):
    """外部アプリで開いた一時ファイルの変更を検知する。"""

    changed = Signal(str, str)  # remote, local

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._schedule_check)
        self._remotes: dict[str, str] = {}
        self._signatures: dict[str, tuple[int, int] | None] = {}
        self._debounce: dict[str, QTimer] = {}
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._poll_files)

    @staticmethod
    def _signature(path: str) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def watch(self, remote: str, local: str):
        local = os.path.abspath(local)
        self._remotes[local] = remote
        self._signatures[local] = self._signature(local)
        if os.path.exists(local) and local not in self._watcher.files():
            self._watcher.addPath(local)
        if not self._poll.isActive():
            self._poll.start()

    def unwatch(self, local: str):
        local = os.path.abspath(local)
        if local in self._watcher.files():
            self._watcher.removePath(local)
        timer = self._debounce.pop(local, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._remotes.pop(local, None)
        self._signatures.pop(local, None)
        if not self._remotes:
            self._poll.stop()

    def stop(self):
        for local in list(self._remotes):
            self.unwatch(local)

    def _schedule_check(self, local: str):
        local = os.path.abspath(local)
        if local not in self._remotes:
            return
        timer = self._debounce.get(local)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda path=local: self._emit_if_changed(path))
            self._debounce[local] = timer
        timer.start(600)

    def _poll_files(self):
        watched = set(self._watcher.files())
        for local in self._remotes:
            if os.path.exists(local) and local not in watched:
                self._watcher.addPath(local)
            if self._signature(local) != self._signatures.get(local):
                self._schedule_check(local)

    def _emit_if_changed(self, local: str):
        signature = self._signature(local)
        if signature is None or signature == self._signatures.get(local):
            return
        self._signatures[local] = signature
        self.changed.emit(self._remotes[local], local)


class SftpWorker(QThread):
    """ジョブキュー方式の SFTP ワーカー。sftp チャネルはこのスレッド内で開く。"""

    listed = Signal(str, list)          # path, entries
    home_resolved = Signal(str)
    precheck_result = Signal(object)    # plan dict + conflicts
    progress = Signal(object)           # {"label","done","total"}
    status = Signal(str)
    error = Signal(str)
    job_done = Signal(str)              # kind
    opened_temp = Signal(str, str)      # remote, local
    opened_for_edit = Signal(str, str)  # remote, local
    editor_save_result = Signal(str, bool, str)  # remote, ok, message
    external_save_result = Signal(str, str, bool, str)  # remote, local, ok, message
    recover_incomplete = Signal(int)    # 未復元件数(sudo が必要)
    worker_failed = Signal(str)

    def __init__(self, session, name: str):
        super().__init__()
        self.session = session
        self.setObjectName(f"sftp-{name}")
        self.q: queue.Queue = queue.Queue()
        self.sftp = None
        self.pm = None                   # 共有 PermManager(ブラウザが設定)
        self.perm_override = False       # 権限無視スイッチ
        self._cancel = False
        self.busy = False
        self._last_emit = 0.0

    # -- 外部 API --------------------------------------------------------
    def enqueue(self, job: dict):
        self.q.put(job)

    def cancel(self):
        self._cancel = True

    def stop(self):
        self.q.put(None)
        self.wait(3000)

    # -- スレッド本体 ------------------------------------------------------
    def run(self):
        try:
            self.sftp = self.session.open_sftp()
        except Exception as e:  # noqa: BLE001
            self.worker_failed.emit(str(e))
            return
        while True:
            job = self.q.get()
            if job is None:
                break
            self._cancel = False
            self.busy = True
            try:
                self._dispatch(job)
            except _Cancelled:
                self.status.emit("キャンセルしました")
                self.progress.emit(IDLE_PROGRESS)
            except Exception as e:  # noqa: BLE001
                self.error.emit(str(e))
                self.progress.emit(IDLE_PROGRESS)
            finally:
                self.busy = False
        try:
            self.sftp.close()
        except Exception:
            pass

    def _dispatch(self, job: dict):
        kind = job["kind"]
        handler = getattr(self, f"_job_{kind}")
        handler(job)

    def _check_cancel(self):
        if self._cancel:
            raise _Cancelled()

    def _emit_progress(self, label: str, done: int, total: int, force=False):
        now = time.monotonic()
        if force or now - self._last_emit > 0.08:
            self._last_emit = now
            self.progress.emit({"label": label, "done": done, "total": max(total, 1)})

    # -- 権限無視ラッパ ---------------------------------------------------
    def _listdir_ov(self, path):
        """listdir_attr を権限無視対応で。弾かれたら一時的に読取許可。"""
        if not self.perm_override:
            return self.sftp.listdir_attr(path)
        holder = {}
        def op():
            holder["r"] = self.sftp.listdir_attr(path)
        self.pm.with_read_access(path, op, is_dir=True)
        return holder["r"]

    def _get_ov(self, remote, local, callback, is_dir=False):
        def _op():
            return self.sftp.get(remote, local, callback=callback)

        if self.perm_override:
            self.pm.with_read_access(remote, _op, is_dir=is_dir)
        else:
            _op()

    def _put_ov(self, local, remote, callback):
        def _op():
            return self.sftp.put(local, remote, callback=callback)

        if self.perm_override:
            self.pm.with_write_access(remote, _op)
        else:
            _op()

    def _job_perm_recover(self, job):
        """前回のプロセスが戻せなかった権限をジャーナルから復元。"""
        if self.pm is None:
            return
        try:
            restored, stuck = self.pm.recover_pending()
            if restored:
                self.status.emit(f"前回未復元の権限を {restored} 件戻しました")
            if stuck:
                self.recover_incomplete.emit(stuck)
        except Exception:
            # 復元失敗は次回接続で再試行されるが、黙って消さないで記録する。
            logger.warning("未復元権限の復元処理に失敗しました", exc_info=True)

    # -- 各ジョブ ---------------------------------------------------------
    def _job_init(self, job):
        home = self.sftp.normalize(".")
        self.home_resolved.emit(home)
        target = job.get("initial") or home
        try:
            self._job_list({"path": target})
        except Exception:
            if target != home:
                self.status.emit(f"初期パス {target} を開けないためホームを表示します")
                self._job_list({"path": home})
            else:
                raise

    def _job_list(self, job):
        path = self.sftp.normalize(job["path"])
        entries = []
        for attr in self._listdir_ov(path):
            mode = attr.st_mode or 0
            entries.append({
                "name": attr.filename,
                "is_dir": statmod.S_ISDIR(mode),
                "is_link": statmod.S_ISLNK(mode),
                "size": attr.st_size,
                "mtime": attr.st_mtime,
                "mode_str": statmod.filemode(mode),
            })
        self.listed.emit(path, entries)

    def _job_mkdir(self, job):
        self.sftp.mkdir(job["path"])
        self.status.emit("フォルダを作成しました")
        self.job_done.emit("mkdir")

    def _job_rename(self, job):
        old, new = job["old"], job["new"]
        try:
            self.sftp.stat(new)
        except IOError:
            pass
        else:
            raise Exception(f"変更できません: {posixpath.basename(new)} は既に存在します")
        self.sftp.rename(old, new)
        self.status.emit("名前を変更しました")
        self.job_done.emit("rename")

    def _job_precheck_upload(self, job):
        conflicts = []
        for local, remote in job["files"]:
            self._check_cancel()
            try:
                attr = self.sftp.stat(remote)
            except IOError:
                continue
            conflicts.append({
                "local": local,
                "remote": remote,
                "r_size": attr.st_size,
                "r_mtime": attr.st_mtime,
                "l_size": os.path.getsize(local) if os.path.exists(local) else 0,
            })
        result = dict(job)
        result["conflicts"] = conflicts
        self.precheck_result.emit(result)

    def _ensure_remote_dirs(self, dirs):
        for d in sorted(set(dirs), key=lambda s: (s.count("/"), s)):
            try:
                self.sftp.stat(d)
            except IOError:
                self.sftp.mkdir(d)

    def _job_upload(self, job):
        files = job["files"]
        self._ensure_remote_dirs(job.get("dirs", []))
        total = sum(
            os.path.getsize(local) for local, _ in files if os.path.exists(local)
        )
        done_before = 0
        for i, (local, remote) in enumerate(files):
            self._check_cancel()
            size = os.path.getsize(local) if os.path.exists(local) else 0
            label = f"アップロード {i + 1}/{len(files)}: {os.path.basename(local)}"

            def cb(sent, _sz, _base=done_before, _label=label):
                self._check_cancel()
                self._emit_progress(_label, _base + sent, total)

            self._put_ov(local, remote, cb)
            done_before += size
        self._emit_progress("", 0, 0, force=True)
        self.progress.emit(IDLE_PROGRESS)
        self.status.emit(f"アップロード完了 ({len(files)} ファイル)")
        self.job_done.emit("upload")

    def _walk_remote(self, rpath: str, lpath: str, root: str, out: list):
        os.makedirs(lpath, exist_ok=True)
        for attr in self._listdir_ov(rpath):
            self._check_cancel()
            r = posixpath.join(rpath, attr.filename)
            local = _safe_local_child(root, lpath, attr.filename)
            mode = attr.st_mode or 0
            if statmod.S_ISDIR(mode):
                self._walk_remote(r, local, root, out)
            else:
                out.append((r, local, attr.st_size or 0))

    def _job_download(self, job):
        plan: list[tuple[str, str, int]] = []  # (remote, local, size)
        destination = job["destination"]
        for remote, name, is_dir in job["items"]:
            self._check_cancel()
            local = _safe_local_child(destination, destination, name)
            if is_dir:
                self._walk_remote(remote, local, destination, plan)
            else:
                try:
                    size = self.sftp.stat(remote).st_size or 0
                except IOError:
                    size = 0
                plan.append((remote, local, size))
        total = sum(s for _, _, s in plan)
        done_before = 0
        for i, (remote, local, size) in enumerate(plan):
            self._check_cancel()
            os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
            label = f"ダウンロード {i + 1}/{len(plan)}: {posixpath.basename(remote)}"

            def cb(got, _sz, _base=done_before, _label=label):
                self._check_cancel()
                self._emit_progress(_label, _base + got, total)

            self._get_ov(remote, local, cb)
            done_before += size
        self.progress.emit(IDLE_PROGRESS)
        self.status.emit(f"ダウンロード完了 ({len(plan)} ファイル)")
        self.job_done.emit("download")

    def _collect_delete(self, path: str, is_dir: bool, out: list):
        """post-order で削除対象を集める。リンクは辿らずファイル扱い。"""
        self._check_cancel()
        if is_dir:
            for attr in self.sftp.listdir_attr(path):
                mode = attr.st_mode or 0
                child = posixpath.join(path, attr.filename)
                child_is_dir = statmod.S_ISDIR(mode) and not statmod.S_ISLNK(mode)
                self._collect_delete(child, child_is_dir, out)
            out.append((path, "d"))
        else:
            out.append((path, "f"))

    def _job_delete(self, job):
        targets: list[tuple[str, str]] = []
        self.status.emit("削除対象を確認中…")
        for path, is_dir in job["items"]:
            self._collect_delete(path, is_dir, targets)
        for i, (path, kind) in enumerate(targets):
            self._check_cancel()
            if kind == "d":
                self.sftp.rmdir(path)
            else:
                self.sftp.remove(path)
            self._emit_progress(f"削除中: {posixpath.basename(path)}", i + 1, len(targets))
        self.progress.emit(IDLE_PROGRESS)
        self.status.emit(f"削除完了 ({len(targets)} 項目)")
        self.job_done.emit("delete")

    def _job_open_temp(self, job):
        remote = job["remote"]
        name = posixpath.basename(remote)
        tmpdir = tempfile.mkdtemp(prefix="hashi_open_")
        local = os.path.join(tmpdir, name)
        size = job.get("size") or 1

        def cb(got, _sz):
            self._check_cancel()
            self._emit_progress(f"開いています: {name}", got, size)

        self._get_ov(remote, local, cb)
        self.progress.emit(IDLE_PROGRESS)
        self.opened_temp.emit(remote, local)

    def _job_open_edit(self, job):
        """内蔵エディタ用に一時 DL。テキストかどうか判定してシグナル。"""
        remote = job["remote"]
        name = posixpath.basename(remote)
        tmpdir = tempfile.mkdtemp(prefix="hashi_edit_")
        local = os.path.join(tmpdir, name)
        size = job.get("size") or 1

        def cb(got, _sz):
            self._check_cancel()
            self._emit_progress(f"開いています: {name}", got, size)

        self._get_ov(remote, local, cb)
        self.progress.emit(IDLE_PROGRESS)
        # バイナリ判定 (先頭に NUL があれば OS アプリで開く)
        try:
            with open(local, "rb") as f:
                head = f.read(8192)
            is_binary = b"\x00" in head
        except Exception:
            logger.debug("バイナリ判定のための読み取りに失敗 (テキスト扱い): %s",
                         local, exc_info=True)
            is_binary = False
        if is_binary:
            self.status.emit("バイナリのため関連付けアプリで開きます")
            self.opened_temp.emit(remote, local)
        else:
            self.opened_for_edit.emit(remote, local)

    def _job_editor_save(self, job):
        """エディタからの保存。上書き確認なしで put (権限無視は尊重)。"""
        remote, local = job["remote"], job["local"]
        try:
            self._put_ov(local, remote, None)
            self.editor_save_result.emit(remote, True, "")
        except (OverrideError, IOError, OSError) as e:
            self.editor_save_result.emit(remote, False, str(e))

    def _job_external_save(self, job):
        """外部アプリによる変更を put (権限無視は尊重)。"""
        remote, local = job["remote"], job["local"]
        try:
            self._put_ov(local, remote, None)
            self.external_save_result.emit(remote, local, True, "")
        except (OverrideError, IOError, OSError) as e:
            self.external_save_result.emit(remote, local, False, str(e))


class _SortItem(QTreeWidgetItem):
    """フォルダ優先 + 列ごとのソートキーで比較する項目。"""

    def __lt__(self, other):
        tree = self.treeWidget()
        col = tree.sortColumn() if tree else 0
        my_dir = bool(self.data(0, Qt.UserRole + 1))
        other_dir = bool(other.data(0, Qt.UserRole + 1))
        if my_dir != other_dir:
            asc = (tree.header().sortIndicatorOrder() == Qt.AscendingOrder) if tree else True
            # 昇順/降順どちらでもフォルダを先頭に保つ
            return my_dir if asc else not my_dir
        a = self.data(col, Qt.UserRole)
        b = other.data(col, Qt.UserRole)
        if a is None:
            a = self.text(col).lower()
        if b is None:
            b = other.text(col).lower()
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class _DropTree(QTreeWidget):
    """OS からのファイルドロップを受けるツリー。"""

    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            ev.acceptProposedAction()


class SftpBrowser(QWidget):
    """SFTP ブラウザ本体。"""

    status_message = Signal(str)

    def __init__(self, session, initial_path: str = "", settings=None,
                 sudo_provider=None, sudo_provider_silent=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.settings = settings
        # override ON 時に sudo パスワードを取得する callable (GUI スレッドで実行)
        self._sudo_provider = sudo_provider or (lambda: None)
        # プロンプトを出さずに保存済み sudo パスワードだけ返す callable
        self._sudo_provider_silent = sudo_provider_silent or (lambda: None)
        self.cwd = ""
        self.home = ""
        self._entries: list[dict] = []
        self._show_hidden = False
        self._editors: dict[str, EditorWindow] = {}   # remote -> window
        self._edit_saves: dict[str, object] = {}       # remote -> done_cb
        self._external_monitor = ExternalFileMonitor(self)
        self._external_monitor.changed.connect(self._save_external_file)

        self._build_ui()

        self.nav = SftpWorker(session, "nav")
        self.xfer = SftpWorker(session, "xfer")
        # 権限無視の調整役を 1 つ作り、両ワーカーで共有(ロック+参照カウント+ジャーナル)
        self.pm = PermManager(
            session, sudo_pw=None, journal=PermJournal(),
            conn_id=session.profile.id_str())
        self.nav.pm = self.pm
        self.xfer.pm = self.pm
        for w in (self.nav, self.xfer):
            w.error.connect(self._on_error)
            w.status.connect(self._on_status)
            w.worker_failed.connect(self._on_worker_failed)
        self.nav.listed.connect(self._on_listed)
        self.nav.home_resolved.connect(self._on_home)
        self.nav.precheck_result.connect(self._on_precheck)
        self.nav.job_done.connect(self._on_job_done)
        self.xfer.progress.connect(self._on_progress)
        self.xfer.job_done.connect(self._on_job_done)
        self.xfer.opened_temp.connect(self._on_opened_temp)
        self.xfer.opened_for_edit.connect(self._on_opened_for_edit)
        self.xfer.editor_save_result.connect(self._on_editor_saved)
        self.xfer.external_save_result.connect(self._on_external_saved)
        self.nav.recover_incomplete.connect(self._on_recover_incomplete)
        self._recover_prompted = False
        self.nav.start()
        self.xfer.start()

        # 前回クラッシュ時に戻せなかった権限をこの接続について復元。
        # root 所有ファイルの復元には sudo が要るので、保存済みがあれば先に渡す。
        silent_pw = self._sudo_provider_silent()
        if silent_pw:
            self.pm.sudo_pw = silent_pw
        self.nav.enqueue({"kind": "perm_recover"})

        # 既定の権限無視状態を反映
        if settings and settings.get("permission_override"):
            self.btn_override.setChecked(True)
        self.nav.enqueue({"kind": "init", "initial": initial_path})

    # ---- UI 構築 ---------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        bar = QHBoxLayout()
        style = self.style()

        def tool(text, tip, slot, icon=None):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedWidth(34 if len(text) <= 2 else 110)
            if icon:
                b.setIcon(style.standardIcon(icon))
                b.setText(text if len(text) > 2 else "")
                b.setFixedWidth(34 if len(text) <= 2 else 120)
            b.clicked.connect(slot)
            bar.addWidget(b)
            return b

        tool("↑", "1つ上のフォルダへ (Backspace)", self.go_up, QStyle.SP_ArrowUp)
        tool("H", "ホームへ", self.go_home, QStyle.SP_DirHomeIcon)
        tool("R", "最新の状態に更新 (F5)", self.refresh, QStyle.SP_BrowserReload)

        self.ed_path = QLineEdit()
        self.ed_path.setPlaceholderText("リモートパス")
        self.ed_path.returnPressed.connect(self._path_entered)
        bar.addWidget(self.ed_path, 1)

        self.btn_hidden = QPushButton("隠しファイル")
        self.btn_hidden.setCheckable(True)
        self.btn_hidden.setToolTip("ドットファイルの表示/非表示")
        self.btn_hidden.toggled.connect(self._toggle_hidden)
        bar.addWidget(self.btn_hidden)
        root.addLayout(bar)

        bar2 = QHBoxLayout()
        b_up = QPushButton("アップロード…")
        b_up.setToolTip("ローカルのファイルを選んで現在のフォルダへ送る (D&D でも可)")
        b_up.clicked.connect(self._pick_upload)
        b_dl = QPushButton("ダウンロード")
        b_dl.setToolTip("選択した項目をローカルへ保存")
        b_dl.clicked.connect(self.download_selected)
        b_new = QPushButton("新規フォルダ")
        b_new.clicked.connect(self.make_dir)
        b_del = QPushButton("削除")
        b_del.setToolTip("選択した項目を削除 (2段階確認あり)")
        b_del.clicked.connect(self.delete_selected)
        for b in (b_up, b_dl, b_new, b_del):
            bar2.addWidget(b)
        bar2.addStretch(1)

        self.btn_override = QPushButton("🔓 権限無視")
        self.btn_override.setCheckable(True)
        self.btn_override.setToolTip(
            "ON にすると、権限で弾かれたファイルを一時的に読み書き可能にして\n"
            "操作し、終わったら即座に元の権限へ戻します。\n"
            "自分が所有者でなければ sudo でパスワードを使って変更します。"
        )
        self.btn_override.toggled.connect(self._toggle_override)
        self.btn_override.setStyleSheet(
            "QPushButton:checked { background:#7a3b3b; font-weight:bold; }")
        bar2.addWidget(self.btn_override)
        root.addLayout(bar2)

        self.tree = _DropTree()
        self.tree.setHeaderLabels(["名前", "サイズ", "更新日時", "属性"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.files_dropped.connect(self.upload_paths)
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 130)
        root.addWidget(self.tree, 1)

        # 進捗パネル
        self.progress_frame = QFrame()
        pf = QHBoxLayout(self.progress_frame)
        pf.setContentsMargins(4, 2, 4, 2)
        self.lb_progress = QLabel("")
        self.pb = QProgressBar()
        self.pb.setMaximumHeight(14)
        b_cancel = QPushButton("中止")
        b_cancel.setFixedWidth(60)
        b_cancel.clicked.connect(lambda: self.xfer.cancel())
        pf.addWidget(self.lb_progress, 1)
        pf.addWidget(self.pb, 1)
        pf.addWidget(b_cancel)
        self.progress_frame.hide()
        root.addWidget(self.progress_frame)

        self.lb_status = QLabel("接続中…")
        self.lb_status.setStyleSheet("color:#8a919e;")
        root.addWidget(self.lb_status)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.lb_status.setText(""))

        QShortcut(QKeySequence(Qt.Key_F5), self, self.refresh)
        QShortcut(QKeySequence(Qt.Key_Delete), self.tree, self.delete_selected)
        QShortcut(QKeySequence(Qt.Key_F2), self.tree, self.rename_selected)
        QShortcut(QKeySequence(Qt.Key_Backspace), self.tree, self.go_up)

    # ---- 一覧表示 ----------------------------------------------------------
    def _on_home(self, home: str):
        self.home = home

    def _on_listed(self, path: str, entries: list):
        self.cwd = path
        self._entries = entries
        self.ed_path.setText(path)
        self._render()

    def _render(self):
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        style = self.style()
        icon_dir = style.standardIcon(QStyle.SP_DirIcon)
        icon_file = style.standardIcon(QStyle.SP_FileIcon)
        icon_link = style.standardIcon(QStyle.SP_FileLinkIcon)
        for e in self._entries:
            if not self._show_hidden and e["name"].startswith("."):
                continue
            item = _SortItem([
                e["name"],
                "" if e["is_dir"] else human_size(e["size"]),
                fmt_mtime(e["mtime"]),
                e["mode_str"],
            ])
            item.setIcon(0, icon_dir if e["is_dir"] else (icon_link if e["is_link"] else icon_file))
            item.setData(0, Qt.UserRole, e["name"].lower())
            item.setData(0, Qt.UserRole + 1, e["is_dir"])
            item.setData(0, Qt.UserRole + 2, e)
            item.setData(1, Qt.UserRole, e["size"] or 0)
            item.setData(2, Qt.UserRole, e["mtime"] or 0)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(item)
        self.tree.setSortingEnabled(True)

    def _on_recover_incomplete(self, stuck: int):
        """前回クラッシュで緩んだ権限が sudo 不足で戻せていないとき。"""
        if self._recover_prompted:
            self._on_status(
                f"前回緩んだ権限が {stuck} 件戻せていません(sudo 権限が必要)")
            return
        self._recover_prompted = True
        r = QMessageBox.question(
            self, "権限の復元",
            f"前回の異常終了で、権限を緩めたまま戻せていないファイルが "
            f"{stuck} 件あります。\nsudo で元の権限に戻しますか?",
        )
        if r != QMessageBox.Yes:
            self._on_status(f"未復元の権限が {stuck} 件残っています")
            return
        pw = self._sudo_provider()  # プロンプト可
        if pw:
            self.pm.sudo_pw = pw
            self.nav.enqueue({"kind": "perm_recover"})
        else:
            self._on_status(f"未復元の権限が {stuck} 件残っています(sudo 未入力)")

    def _toggle_hidden(self, on: bool):
        self._show_hidden = on
        self._render()

    def _toggle_override(self, on: bool):
        if on:
            pw = self._sudo_provider()  # None でも SFTP chmod は試せる
            self.pm.sudo_pw = pw
            self._on_status(
                "権限無視 ON: 弾かれたら一時的に権限を付与→操作→即復元します"
                " (元の権限はジャーナルに記録され、異常終了しても次回復元)"
                + ("" if pw else " / sudoパスワード未設定: 自分が所有するファイルのみ"))
        else:
            self._on_status("権限無視 OFF")
        self.nav.perm_override = on
        self.xfer.perm_override = on
        if on:
            # sudo が使えるようになったので、root 所有の未復元分も片付ける
            self.nav.enqueue({"kind": "perm_recover"})
        if self.settings:
            self.settings.set("permission_override", on)

    def _selected_entries(self) -> list[dict]:
        return [it.data(0, Qt.UserRole + 2) for it in self.tree.selectedItems()]

    # ---- ナビゲーション -------------------------------------------------------
    def cd(self, path: str):
        self.nav.enqueue({"kind": "list", "path": path})

    def refresh(self):
        if self.cwd:
            self.cd(self.cwd)

    def go_up(self):
        if self.cwd and self.cwd != "/":
            self.cd(posixpath.dirname(self.cwd.rstrip("/")) or "/")

    def go_home(self):
        if self.home:
            self.cd(self.home)

    def _path_entered(self):
        path = self.ed_path.text().strip()
        if path:
            self.cd(path)

    def _double_clicked(self, item, _col):
        e = item.data(0, Qt.UserRole + 2)
        if e is None:
            return
        full = posixpath.join(self.cwd, e["name"])
        if e["is_dir"] or e["is_link"]:
            self.cd(full)
            return
        size = e["size"] or 0
        # 既に開いているエディタがあれば前面へ
        if full in self._editors:
            self._editors[full].raise_()
            self._editors[full].activateWindow()
            return
        use_editor = (
            self.settings and self.settings.get("open_text_in_editor")
            and self._looks_text(e["name"]) and size <= EDIT_SIZE_LIMIT
        )
        if use_editor:
            self.xfer.enqueue({"kind": "open_edit", "remote": full, "size": size})
            return
        if size > OPEN_SIZE_WARN:
            r = QMessageBox.question(
                self, "サイズ確認",
                f"{e['name']} は {human_size(size)} あります。\n"
                "一時ダウンロードして開きますか?",
            )
            if r != QMessageBox.Yes:
                return
        self.xfer.enqueue({"kind": "open_temp", "remote": full, "size": size})

    @staticmethod
    def _looks_text(name: str) -> bool:
        base = name.lower()
        if base in TEXT_EXTS:  # 拡張子なしの既知名 (Makefile 等)
            return True
        ext = base.rsplit(".", 1)[-1] if "." in base else ""
        return ext in TEXT_EXTS or "." not in base

    def _on_opened_for_edit(self, remote: str, local: str):
        try:
            win = EditorWindow(remote, local, self._save_from_editor,
                               self.settings, parent=self)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "エディタ", f"開けませんでした:\n{e}")
            return
        win.setAttribute(Qt.WA_DeleteOnClose, False)
        win.closed.connect(self._on_editor_closed)
        self._editors[remote] = win
        win.show()
        win.raise_()
        self._on_status(f"内蔵エディタで開きました: {posixpath.basename(remote)}")

    def _save_from_editor(self, remote: str, local: str, done_cb):
        """エディタの Ctrl+S から呼ばれる。上書き確認なしでアップロード。"""
        self._edit_saves[remote] = done_cb
        self.xfer.enqueue({"kind": "editor_save", "remote": remote, "local": local})

    def _on_editor_saved(self, remote: str, ok: bool, message: str):
        cb = self._edit_saves.pop(remote, None)
        if cb is not None:
            cb(ok, message)
        if ok:
            self._on_status(f"保存しました: {remote}")
            if self.cwd and posixpath.dirname(remote) == self.cwd:
                self.refresh()

    def _on_editor_closed(self, win):
        for remote, w in list(self._editors.items()):
            if w is win:
                del self._editors[remote]

    def _on_opened_temp(self, remote: str, local: str):
        self._external_monitor.watch(remote, local)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(local)):
            self._external_monitor.unwatch(local)
            QMessageBox.warning(
                self, "関連付けアプリ", f"ファイルを開けませんでした:\n{local}")
            return
        self._on_status(
            f"関連付けアプリで開きました (変更は自動保存): {os.path.basename(local)}")

    def _save_external_file(self, remote: str, local: str):
        self._on_status(f"変更を検出しました。アップロード中: {posixpath.basename(remote)}")
        self.xfer.enqueue({"kind": "external_save", "remote": remote, "local": local})

    def _on_external_saved(self, remote: str, _local: str, ok: bool, message: str):
        if not ok:
            QMessageBox.warning(
                self, "自動アップロード",
                f"変更をリモートへ保存できませんでした:\n{remote}\n\n{message}")
            return
        self._on_status(f"変更をリモートへ保存しました: {remote}")
        if self.cwd and posixpath.dirname(remote) == self.cwd:
            self.refresh()

    # ---- フォルダ作成 / リネーム ------------------------------------------------
    def make_dir(self):
        name, ok = QInputDialog.getText(self, "新規フォルダ", "フォルダ名:")
        name = name.strip()
        if ok and name:
            if "/" in name:
                QMessageBox.warning(self, "新規フォルダ", "フォルダ名に / は使えません。")
                return
            self.nav.enqueue({"kind": "mkdir",
                              "path": posixpath.join(self.cwd, name)})

    def rename_selected(self):
        sel = self._selected_entries()
        if len(sel) != 1:
            return
        e = sel[0]
        new, ok = QInputDialog.getText(self, "名前の変更", "新しい名前:",
                                       text=e["name"])
        new = new.strip()
        if ok and new and new != e["name"]:
            if "/" in new:
                QMessageBox.warning(self, "名前の変更", "名前に / は使えません。")
                return
            self.nav.enqueue({
                "kind": "rename",
                "old": posixpath.join(self.cwd, e["name"]),
                "new": posixpath.join(self.cwd, new),
            })

    # ---- 削除 (2 段階確認) ------------------------------------------------------
    def delete_selected(self):
        sel = self._selected_entries()
        if not sel:
            return
        names = [e["name"] for e in sel]
        shown = "<br>".join(f"・{escape(n)}" for n in names[:8])
        if len(names) > 8:
            shown += f"<br>… ほか {len(names) - 8} 件"
        # 1 段階目: 対象の一覧を見せて確認
        box = QMessageBox(self)
        box.setWindowTitle("削除の確認 (1/2)")
        box.setTextFormat(Qt.RichText)
        box.setText(
            f"リモートから <b>{len(names)} 個</b> の項目を削除します。"
            f"<br><br>{shown}<br><br>フォルダは中身ごと削除されます。"
        )
        box.setIcon(QMessageBox.Warning)
        next_btn = box.addButton("次へ", QMessageBox.AcceptRole)
        box.addButton("キャンセル", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not next_btn:
            return
        # 2 段階目: 確認語の入力
        if not DoubleCheckDialog.confirm(
            self, "削除の確認 (2/2)",
            "<b style='color:#e06c75;'>この操作は取り消せません。</b>",
            "delete", "削除する",
        ):
            return
        items = [
            (posixpath.join(self.cwd, e["name"]), e["is_dir"] and not e["is_link"])
            for e in sel
        ]
        self.xfer.enqueue({"kind": "delete", "items": items})

    # ---- アップロード (上書きは 2 段階確認) ------------------------------------------
    def _pick_upload(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "アップロードするファイルを選択")
        if paths:
            self.upload_paths(paths)

    def upload_paths(self, paths: list[str]):
        if not self.cwd:
            return
        files, dirs = expand_local(paths, self.cwd)
        if not files and not dirs:
            return
        self._on_status("既存ファイルとの競合を確認中…")
        self.nav.enqueue({
            "kind": "precheck_upload",
            "files": files, "dirs": dirs,
        })

    def _on_precheck(self, plan: dict):
        conflicts = plan["conflicts"]
        files, dirs = plan["files"], plan["dirs"]
        if not conflicts:
            self.xfer.enqueue({"kind": "upload", "files": files, "dirs": dirs})
            return
        rows = []
        for c in conflicts[:8]:
            rows.append(
                f"・{escape(posixpath.basename(c['remote']))} "
                f"(リモート {human_size(c['r_size'])} {fmt_mtime(c['r_mtime'])}"
                f" → 新 {human_size(c['l_size'])})"
            )
        shown = "<br>".join(rows)
        if len(conflicts) > 8:
            shown += f"<br>… ほか {len(conflicts) - 8} 件"
        # 1 段階目
        box = QMessageBox(self)
        box.setWindowTitle("上書きの確認 (1/2)")
        box.setTextFormat(Qt.RichText)
        box.setIcon(QMessageBox.Warning)
        box.setText(
            f"<b>{len(conflicts)} 個</b> のファイルがリモートに既に存在します。"
            f"<br><br>{shown}"
        )
        b_over = box.addButton("上書きへ進む", QMessageBox.AcceptRole)
        b_skip = box.addButton("競合をスキップして送る", QMessageBox.ActionRole)
        box.addButton("キャンセル", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_over:
            # 2 段階目
            if not DoubleCheckDialog.confirm(
                self, "上書きの確認 (2/2)",
                "<b style='color:#e06c75;'>リモートの既存ファイルが新しい内容で置き換わります。</b>",
                "overwrite", "上書きする",
            ):
                return
            self.xfer.enqueue({"kind": "upload", "files": files, "dirs": dirs})
        elif clicked is b_skip:
            conflict_remotes = {c["remote"] for c in conflicts}
            remain = [
                (local, remote)
                for local, remote in files
                if remote not in conflict_remotes
            ]
            if not remain and not dirs:
                self._on_status("送信するファイルがありません")
                return
            self.xfer.enqueue({"kind": "upload", "files": remain, "dirs": dirs})

    # ---- ダウンロード (ローカル上書きも 2 段階確認) ------------------------------------
    def download_selected(self):
        sel = self._selected_entries()
        if not sel:
            self._on_status("ダウンロードする項目を選択してください")
            return
        dest = QFileDialog.getExistingDirectory(self, "保存先フォルダを選択")
        if not dest:
            return
        items = []
        conflicts = []
        try:
            for e in sel:
                remote = posixpath.join(self.cwd, e["name"])
                is_dir = e["is_dir"] and not e["is_link"]
                local = _safe_local_child(dest, dest, e["name"])
                if os.path.exists(local):
                    conflicts.append((e["name"], is_dir))
                items.append((remote, e["name"], is_dir))
        except ValueError as e:
            QMessageBox.warning(self, "ダウンロード", str(e))
            return
        if conflicts:
            shown = "<br>".join(
                f"・{escape(n)}"
                f"{' (フォルダ: 統合され同名ファイルは上書き)' if d else ''}"
                for n, d in conflicts[:8]
            )
            if len(conflicts) > 8:
                shown += f"<br>… ほか {len(conflicts) - 8} 件"
            box = QMessageBox(self)
            box.setWindowTitle("ローカル上書きの確認 (1/2)")
            box.setTextFormat(Qt.RichText)
            box.setIcon(QMessageBox.Warning)
            box.setText(
                f"保存先に同名の項目が <b>{len(conflicts)} 個</b> あります。"
                f"<br><br>{shown}"
            )
            b_over = box.addButton("上書きへ進む", QMessageBox.AcceptRole)
            box.addButton("キャンセル", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not b_over:
                return
            if not DoubleCheckDialog.confirm(
                self, "ローカル上書きの確認 (2/2)",
                "<b style='color:#e06c75;'>ローカルの既存ファイルが置き換わります。</b>",
                "overwrite", "上書きする",
            ):
                return
        self.xfer.enqueue({
            "kind": "download",
            "destination": dest,
            "items": items,
        })

    # ---- コンテキストメニュー ------------------------------------------------------
    def _context_menu(self, pos):
        sel = self._selected_entries()
        menu = QMenu(self)
        one_file = len(sel) == 1 and not sel[0]["is_dir"]
        a_edit = menu.addAction("内蔵エディタで開く")
        a_edit.setEnabled(one_file)
        a_open = menu.addAction("関連付けアプリで開く")
        a_open.setEnabled(one_file)
        menu.addSeparator()
        a_dl = menu.addAction("ダウンロード")
        a_ren = menu.addAction("名前の変更 (F2)")
        a_del = menu.addAction("削除 (Del)")
        menu.addSeparator()
        a_new = menu.addAction("新規フォルダ")
        a_copy = menu.addAction("パスをコピー")
        a_ref = menu.addAction("更新 (F5)")
        a_dl.setEnabled(bool(sel))
        a_ren.setEnabled(len(sel) == 1)
        a_del.setEnabled(bool(sel))
        a_copy.setEnabled(bool(sel))
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is a_edit and one_file:
            full = posixpath.join(self.cwd, sel[0]["name"])
            if full in self._editors:
                self._editors[full].raise_()
            else:
                self.xfer.enqueue({"kind": "open_edit", "remote": full,
                                   "size": sel[0]["size"] or 0})
        elif chosen is a_open and one_file:
            self.xfer.enqueue({"kind": "open_temp",
                               "remote": posixpath.join(self.cwd, sel[0]["name"]),
                               "size": sel[0]["size"] or 0})
        elif chosen is a_dl:
            self.download_selected()
        elif chosen is a_ren:
            self.rename_selected()
        elif chosen is a_del:
            self.delete_selected()
        elif chosen is a_new:
            self.make_dir()
        elif chosen is a_ref:
            self.refresh()
        elif chosen is a_copy:
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.clipboard().setText(
                "\n".join(posixpath.join(self.cwd, e["name"]) for e in sel)
            )

    # ---- 進捗/ステータス ----------------------------------------------------------
    def _on_progress(self, info: dict):
        if not info["label"] and info["total"] <= 1:
            self.progress_frame.hide()
            return
        self.progress_frame.show()
        self.lb_progress.setText(info["label"])
        self.pb.setMaximum(info["total"])
        self.pb.setValue(min(info["done"], info["total"]))

    def _on_status(self, msg: str):
        self.lb_status.setText(msg)
        self._status_timer.start(5000)
        self.status_message.emit(msg)

    def _on_error(self, msg: str):
        self.progress_frame.hide()
        QMessageBox.warning(self, "SFTP エラー", msg)

    def _on_worker_failed(self, msg: str):
        QMessageBox.critical(self, "SFTP", f"SFTP チャネルを開けませんでした:\n{msg}")

    def _on_job_done(self, kind: str):
        if kind in ("upload", "delete", "mkdir", "rename"):
            self.refresh()

    # ---- 後始末 -------------------------------------------------------------------
    def has_active_transfers(self) -> bool:
        return self.xfer.busy or not self.xfer.q.empty()

    def shutdown(self):
        for win in list(self._editors.values()):
            win.close()
        self._external_monitor.stop()
        self.xfer.cancel()
        self.nav.stop()
        self.xfer.stop()
        if self.pm is not None:
            self.pm.close()
