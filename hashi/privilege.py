"""権限無視スイッチ (Permission Override) のコア。

考え方: 通常どおり SFTP 操作を試み、"Permission denied" で弾かれたときだけ
一時的に権限を緩め、操作し、**必ず元の権限へ戻す**。緩める前にジャーナルへ
「元の権限」を fsync 記録しておくので、途中でプロセスが強制終了されても
次回接続時に復元できる。

このマネージャは 1 接続につき 1 つだけ作り、nav / xfer 両方のワーカーで
**共有**する。内部にロック + 参照カウントを持ち、両ワーカーが同じファイルの
権限を同時にいじっても、元の権限を取り違えたり二重に戻したりしない。
chmod / stat 用に専用の SFTP チャネルを 1 本持ち、その利用はすべてロックで
直列化する(paramiko の 1 チャネルはスレッド安全でないため)。実際の
get/put はワーカー自身の SFTP チャネルで行うので、転送は並行できる。

chmod はまず SFTP で試し(自分が所有者なら成功)、ダメなら SSH の
`sudo -S chmod` にフォールバックする。
"""
from __future__ import annotations

import errno
import os
import posixpath
import shlex
import stat as statmod
import threading

from .permjournal import PermJournal, pid_alive

# 追加で付与するビット。接続ユーザーが所有者とは限らないため other も含める
_READ_BITS = 0o444    # a+r
_WRITE_BITS = 0o222   # a+w
_DIR_ENTER_BITS = 0o111  # a+x (ディレクトリを辿る/作る)


class OverrideError(Exception):
    """権限昇格に失敗した(sudo 不可 / パスワード違い等)。"""


def is_permission_error(exc: Exception) -> bool:
    if isinstance(exc, PermissionError):
        return True
    en = getattr(exc, "errno", None)
    if en in (errno.EACCES, errno.EPERM):
        return True
    msg = str(exc).lower()
    return "permission denied" in msg or "not permitted" in msg


class PermManager:
    """権限無視の調整役。1 接続で 1 インスタンス、両ワーカーで共有する。"""

    def __init__(self, session, sudo_pw: str | None = None,
                 journal: PermJournal | None = None, conn_id: str = ""):
        self.session = session
        self.sudo_pw = sudo_pw               # 外部(ブラウザ)から差し替え可
        self.journal = journal or PermJournal()
        self.conn_id = conn_id
        self._pid = os.getpid()
        self._lock = threading.RLock()       # 再入可(内部呼び出しが入れ子)
        self._sftp = None                    # chmod/stat 専用チャネル(遅延生成)
        # key: remote path -> {"orig":int, "granted":int, "count":int, "eid":str}
        self._active: dict[str, dict] = {}
        self.restore_errors: list[str] = []

    # ---- 専用 SFTP チャネル(ロック下でのみ使用) ----------------------------
    def _ensure_sftp(self):
        if self._sftp is None:
            self._sftp = self.session.open_sftp()
        return self._sftp

    def close(self):
        with self._lock:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                except Exception:
                    pass
                self._sftp = None

    # ---- 低レベル(すべてロック下で呼ぶこと) -------------------------------
    def get_mode(self, path: str) -> int | None:
        try:
            return statmod.S_IMODE(self._ensure_sftp().stat(path).st_mode or 0)
        except IOError:
            rc, out, _ = self._sudo(f"stat -c '%a' {shlex.quote(path)}")
            if rc == 0 and out.strip():
                try:
                    return int(out.strip(), 8)
                except ValueError:
                    return None
            return None

    def _sudo(self, command: str):
        rc, out, err = self.session.run_sudo(command, self.sudo_pw)
        return rc, out, err

    def chmod(self, path: str, mode: int) -> str:
        """chmod を実行。SFTP → sudo の順。成功時 "sftp"/"sudo" を返す。"""
        try:
            self._ensure_sftp().chmod(path, mode)
            return "sftp"
        except IOError as e:
            if not is_permission_error(e):
                raise
        rc, _out, err = self._sudo(f"chmod {format(mode, 'o')} {shlex.quote(path)}")
        if rc == 0:
            return "sudo"
        raise OverrideError(
            f"権限変更に失敗しました ({path}).\n"
            f"sudo も使えないか、パスワードが違います。\n{err.strip()}")

    # ---- 参照カウント付き acquire / release --------------------------------
    def _acquire(self, path: str, add_bits: int):
        """path に add_bits を一時付与(ジャーナル記録込み)。冪等に refcount。"""
        with self._lock:
            e = self._active.get(path)
            if e is not None:
                e["count"] += 1
                if (e["granted"] & add_bits) != add_bits:
                    e["granted"] |= add_bits
                    self.chmod(path, e["orig"] | e["granted"])
                return
            orig = self.get_mode(path)
            if orig is None:
                raise OverrideError(f"権限を取得できませんでした: {path}")
            if (orig & add_bits) == add_bits:
                # 既に十分な権限がある。触らず、戻す対象にもしない
                self._active[path] = {"orig": orig, "granted": 0,
                                      "count": 1, "eid": None}
                return
            # 緩める前にジャーナルへ元の権限を記録(fsync)
            eid = self.journal.record(self.conn_id, path, orig, self._pid)
            try:
                self.chmod(path, orig | add_bits)
            except Exception:
                self.journal.clear(eid)
                raise
            self._active[path] = {"orig": orig, "granted": add_bits,
                                  "count": 1, "eid": eid}

    def _release(self, path: str):
        with self._lock:
            e = self._active.get(path)
            if e is None:
                return
            e["count"] -= 1
            if e["count"] > 0:
                return
            try:
                if e["granted"]:            # 実際に緩めたものだけ戻す
                    self.chmod(path, e["orig"])
            except Exception as ex:  # noqa: BLE001
                self.restore_errors.append(f"{path}: {ex}")
            finally:
                if e["eid"]:
                    self.journal.clear(e["eid"])
                del self._active[path]

    # ---- 読み取りを保証して op を実行 --------------------------------------
    def with_read_access(self, path: str, op, is_dir: bool = False):
        try:
            return op()
        except IOError as e:
            if not is_permission_error(e):
                raise
        grants = self._read_grant_plan(path, is_dir)
        acquired: list[str] = []
        try:
            for p, bits in grants:
                try:
                    self._acquire(p, bits)
                    acquired.append(p)
                except OverrideError:
                    if p == path:
                        raise            # 目的のパスは失敗なら諦める
                    # 子は best-effort
            return op()
        finally:
            for p in reversed(acquired):
                self._release(p)

    def _read_grant_plan(self, path: str, is_dir: bool):
        """緩めるべき (path, bits) の一覧。ディレクトリは 1 階層下も先回り。"""
        plan = [(path, _READ_BITS | (_DIR_ENTER_BITS if is_dir else 0))]
        if is_dir:
            with self._lock:
                try:
                    for name in self._list_maybe_sudo(path):
                        child = posixpath.join(path, name)
                        cbits = _READ_BITS | (
                            _DIR_ENTER_BITS if self._is_dir_maybe_sudo(child) else 0)
                        plan.append((child, cbits))
                except Exception:
                    pass
        return plan

    # ---- 書き込みを保証して op を実行 --------------------------------------
    def with_write_access(self, remote_path: str, op):
        try:
            return op()
        except IOError as e:
            if not is_permission_error(e):
                raise
        target, bits = self._write_target(remote_path)
        self._acquire(target, bits)
        try:
            return op()
        finally:
            self._release(target)

    def _write_target(self, remote_path: str):
        with self._lock:
            exists = True
            try:
                self._ensure_sftp().stat(remote_path)
            except IOError:
                exists = self._exists_maybe_sudo(remote_path)
        if exists:
            return remote_path, _WRITE_BITS
        parent = posixpath.dirname(remote_path.rstrip("/")) or "/"
        return parent, _WRITE_BITS | _DIR_ENTER_BITS

    # ---- クラッシュ後の復元 -------------------------------------------------
    def recover_pending(self):
        """前回のプロセスが戻せなかった権限を復元。(復元数, 未復元数) を返す。

        未復元 = 死んだ pid のエントリなのに chmod が権限不足等で失敗したもの
        (= sudo が必要だが sudo パスワードが無い等)。呼び出し側はこれを見て
        sudo パスワードを入手し、再度この関数を呼べば残りを片付けられる。
        """
        if not self.conn_id or not self.journal.has_pending(self.conn_id):
            return (0, 0)
        entries = [e for e in self.journal.pending_for(self.conn_id)
                   if not pid_alive(e.get("pid"))]
        # 深いパスから戻す(親ディレクトリの x を先に外して子へ辿れなくなるのを防ぐ)
        entries.sort(key=lambda e: e["path"].count("/"), reverse=True)
        restored, stuck = 0, 0
        for e in entries:
            try:
                with self._lock:
                    self.chmod(e["path"], int(e["orig"]))
                self.journal.clear(e["id"])
                restored += 1
            except Exception:
                stuck += 1  # 次回 or sudo 入手後に持ち越し
        return (restored, stuck)

    # ---- sudo 補助(ロック下で呼ぶこと) ------------------------------------
    def _list_maybe_sudo(self, path):
        try:
            return self._ensure_sftp().listdir(path)
        except IOError:
            rc, out, _ = self._sudo(f"ls -1a {shlex.quote(path)}")
            if rc == 0:
                return [n for n in out.splitlines() if n not in (".", "..")]
            return []

    def _is_dir_maybe_sudo(self, path) -> bool:
        try:
            return statmod.S_ISDIR(self._ensure_sftp().stat(path).st_mode or 0)
        except IOError:
            rc, out, _ = self._sudo(
                f"test -d {shlex.quote(path)} && echo D || echo F")
            return rc == 0 and out.strip() == "D"

    def _exists_maybe_sudo(self, path) -> bool:
        rc, out, _ = self._sudo(
            f"test -e {shlex.quote(path)} && echo Y || echo N")
        return rc == 0 and out.strip() == "Y"
