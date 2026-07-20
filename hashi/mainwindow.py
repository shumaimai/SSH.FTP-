"""メインウィンドウ。

左: 接続プロファイル一覧 / 右: セッションタブ
各タブ = ターミナル + SFTP ブラウザ (QSplitter で並列表示、片方だけの表示も可)
接続処理はワーカースレッドで行い、パスワード入力やホスト鍵確認だけ
GUI スレッドに問い合わせる。
"""
from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import netadmin, p2p, portability, sshd_admin, style
from .config import APP_VERSION, KnownHosts, Profile, ProfileStore, Settings
from .credentials import CredentialStore
from .dialogs import (
    ConnectDialog,
    DoubleCheckDialog,
    HostKeyDialog,
    KeygenDialog,
    NetAdminDialog,
    P2PSendDialog,
    SasConfirmDialog,
    SecretDialog,
    SettingsDialog,
    SnippetsManageDialog,
    SnippetVariablesDialog,
    SshdHardenDialog,
    TunnelDialog,
    ask_secret,
)
from .filebrowser import SftpBrowser
from .forward import DynamicForward, Forward, LocalForward, RemoteForward
from .keygen import generate_key, register_public_key
from .sessionlog import SessionLog
from .snippets import Snippet, SnippetStore, expand_snippet
from .ssh_core import ConnectCancelled, SshSession
from .terminal import TerminalWidget
from .updatecheck import UpdateCheckWorker
from .windowfit import fit_to_screen

logger = logging.getLogger(__name__)


class ConnectWorker(QThread):
    """接続処理スレッド。秘密情報の入力は GUI に signal で依頼してブロック待機。

    保存済みの認証情報があれば自動で使い、なければ入力を求める。
    入力時に「保存する」が選ばれたら CredentialStore へ書き込む。
    """

    # prompt, default_save, can_save
    ask_secret = Signal(str, bool, bool)
    ask_hostkey = Signal(object)
    ok = Signal(object)      # SshSession
    fail = Signal(str)       # 空文字 = キャンセル(静かに終了)

    def __init__(self, profile: Profile, known_hosts: KnownHosts,
                 credentials: CredentialStore | None = None,
                 settings: Settings | None = None):
        super().__init__()
        self.profile = profile
        self.known_hosts = known_hosts
        self.credentials = credentials
        self.settings = settings
        self._evt = threading.Event()
        self._resp = None
        self._tried_kinds: set[str] = set()
        self._saved_kinds: set[str] = set()   # 保存済みストアから読んで使った種別
        self.used_password: str | None = None
        self.used_passphrase: str | None = None

    def provide(self, value):
        """GUI スレッドから応答を返す。"""
        self._resp = value
        self._evt.set()

    def _blocking_ask(self, emit_fn):
        self._evt.clear()
        emit_fn()
        self._evt.wait()
        return self._resp

    # ssh_core が要求する ui インターフェース
    def get_secret(self, prompt: str):
        normalized = prompt.lower()
        kind = (
            "passphrase"
            if "passphrase" in normalized or "パスフレーズ" in prompt
            else "password"
        )
        # 1) 保存済みを最初の 1 回だけ試す。ただし踏み台(ProxyJump)への
        #    プロンプトは別ホストなので、接続先の保存済みパスワードを流用しない
        #    (プロンプト文字列に「踏み台」が含まれるのが ssh_core との取り決め)
        is_jump = "踏み台" in prompt
        if self.credentials and kind not in self._tried_kinds and not is_jump:
            self._tried_kinds.add(kind)
            saved = self.credentials.get(self.profile, kind)
            if saved:
                self._saved_kinds.add(kind)
                self._remember(kind, saved)
                return saved
        # 2) 入力を求める。踏み台の秘密は接続先プロファイルのキーで保存すると
        #    汚染される(次回、接続先に踏み台のパスワードを自動送信してしまう)
        #    ので保存させない。sudo 供給源(used_password)にもしない。
        default_save = self.profile.save_secrets and not is_jump
        can_save = bool(self.credentials and self.credentials.available
                        and not is_jump)
        resp = self._blocking_ask(
            lambda: self.ask_secret.emit(prompt, default_save, can_save))
        secret, save = resp if isinstance(resp, tuple) else (resp, False)
        if secret is None:
            return None
        if save and self.credentials and not is_jump:
            self.credentials.set(self.profile, kind, secret)
        if not is_jump:
            self._remember(kind, secret)
        return secret

    def _remember(self, kind, secret):
        if kind == "password":
            self.used_password = secret
        else:
            self.used_passphrase = secret

    def confirm_hostkey(self, info: dict) -> bool:
        return bool(self._blocking_ask(lambda: self.ask_hostkey.emit(info)))

    def run(self):
        session = SshSession(self.profile, self.known_hosts)
        if self.settings:
            session.keepalive = self.settings.get("keepalive_interval")
        try:
            session.connect(self)
            self.ok.emit(session)
        except ConnectCancelled:
            self.fail.emit("")
        except Exception as e:  # noqa: BLE001
            # 認証失敗時、保存済みが誤りだった可能性 → 使った保存済み認証情報を消す
            # (次回また同じ誤りで自動失敗し続けるのを防ぐ)
            # ssh_core のエラーメッセージは日本語(「〜認証に失敗しました。」)なので
            # "auth" だけでは一致しない。「認証」も見る。
            msg = str(e).lower()
            if self.credentials and ("auth" in msg or "認証" in msg):
                for kind in self._saved_kinds:
                    self.credentials.delete(self.profile, kind)
                    logger.info("認証失敗のため保存済み %s を削除しました", kind)
            self.fail.emit(str(e))


class KeygenWorker(QThread):
    """鍵生成と公開鍵登録を GUI スレッド外で実行する。"""

    ok = Signal(str, bool)
    fail = Signal(str)

    def __init__(self, settings: dict, session: SshSession | None = None):
        super().__init__()
        self.settings = settings
        self.session = session

    def run(self):
        saved = False
        try:
            generated = generate_key(
                self.settings["key_type"],
                self.settings["bits"],
                self.settings["passphrase"],
                self.settings["comment"],
            )
            generated.write_private_key(
                self.settings["path"], self.settings["passphrase"]
            )
            saved = True
            registered = False
            if self.settings["register"] and self.session is not None:
                registered = register_public_key(self.session, generated.public_line)
            self.ok.emit(self.settings["path"], registered)
        except Exception as e:  # noqa: BLE001
            logger.warning("SSH 鍵の生成または登録に失敗しました", exc_info=True)
            if saved:
                self.fail.emit(
                    "秘密鍵の保存は完了しました。公開鍵の登録のみ失敗しました。\n"
                    f"{e}"
                )
            else:
                self.fail.emit(str(e))


class SshdHardenWorker(QThread):
    """sshd 設定変更(Issue #12)を GUI スレッド外で実行する。

    鍵ログインの事前検証・変更後の疎通確認は、いずれも別接続を張るブロッキング
    処理なのでこのワーカースレッド内で行う(GUI を固めない)。
    """

    ok = Signal(dict)
    fail = Signal(str)

    def __init__(self, session, profile, known_hosts, credentials,
                 sudo_pw, changes):
        super().__init__()
        self.session = session
        self.profile = profile
        self.known_hosts = known_hosts
        self.credentials = credentials
        self.sudo_pw = sudo_pw
        self.changes = changes

    def _verify_ui(self):
        creds = self.credentials
        profile = self.profile

        class _Ui:
            def get_secret(self, prompt):
                kind = ("passphrase"
                        if "パスフレーズ" in prompt or "passphrase" in prompt.lower()
                        else "password")
                return creds.get(profile, kind) if creds else None

            def confirm_hostkey(self, info):
                # 同一ホストの再接続。TOFU 記録があれば一致し確認は呼ばれない。
                return True

        return _Ui()

    def _verify_key_login(self) -> bool:
        from dataclasses import replace

        from .config import AUTH_KEY
        if not self.profile.key_path:
            return False
        p = replace(self.profile, auth_method=AUTH_KEY)
        sess = SshSession(p, self.known_hosts)
        try:
            sess.connect(self._verify_ui())
            ok = sess.is_alive()
            sess.close()
            return ok
        except Exception:
            logger.info("鍵ログインの事前検証に失敗", exc_info=True)
            return False

    def _verify_reachable(self, port) -> bool:
        from dataclasses import replace
        target_port = port if port is not None else self.profile.port
        p = replace(self.profile, port=target_port)
        sess = SshSession(p, self.known_hosts)
        try:
            sess.connect(self._verify_ui())
            ok = sess.is_alive()
            sess.close()
            return ok
        except Exception:
            logger.info("変更後の疎通確認に失敗 (port=%s)", target_port, exc_info=True)
            return False

    def run(self):
        try:
            self.session._hashi_sudo_pw = self.sudo_pw
            res = sshd_admin.apply_changes(
                self.session,
                disable_password=self.changes.get("disable_password"),
                new_port=self.changes.get("new_port"),
                verify_key_login=self._verify_key_login,
                verify_reachable=self._verify_reachable,
            )
            self.ok.emit(res)
        except sshd_admin.SshdAdminError as e:
            self.fail.emit(str(e))
        except Exception as e:  # noqa: BLE001
            logger.warning("sshd 設定変更で予期しない例外", exc_info=True)
            self.fail.emit(f"予期しないエラー: {e}")


class NetAdminWorker(QThread):
    """静的 IP 設定(Issue #45)を GUI スレッド外で実行する。

    適用後の疎通確認は「新しい IP へ別接続を張る」ブロッキング処理なので、
    このワーカースレッド内で行う(GUI を固めない)。
    """

    ok = Signal(dict)
    fail = Signal(str)

    def __init__(self, session, profile, known_hosts, credentials,
                 sudo_pw, settings):
        super().__init__()
        self.session = session
        self.profile = profile
        self.known_hosts = known_hosts
        self.credentials = credentials
        self.sudo_pw = sudo_pw
        self.settings = settings

    def _verify_ui(self):
        creds = self.credentials
        profile = self.profile

        class _Ui:
            def get_secret(self, prompt):
                kind = ("passphrase"
                        if "パスフレーズ" in prompt or "passphrase" in prompt.lower()
                        else "password")
                return creds.get(profile, kind) if creds else None

            def confirm_hostkey(self, info):
                # 新 IP は初見ホストになりうる。IP 固定の疎通確認目的なので信頼して続行。
                return True

        return _Ui()

    def _connect_new(self, new_ip):
        """新 IP へ別接続を張って返す(失敗時 None)。"""
        from dataclasses import replace
        p = replace(self.profile, host=new_ip)
        sess = SshSession(p, self.known_hosts)
        try:
            sess.connect(self._verify_ui())
            if sess.is_alive():
                return sess
            sess.close()
        except Exception:
            logger.info("新 IP %s への接続に失敗", new_ip, exc_info=True)
        return None

    def _verify_reachable(self, new_ip) -> bool:
        sess = self._connect_new(new_ip)
        if sess is None:
            return False
        sess.close()
        return True

    def _post_confirm(self, new_ip) -> list:
        """確定後の後片付け: 新 IP 側の接続から残留アドレスを掃除する。

        旧 IP はここで剥がれる(=旧 IP 経由のセッションは切れる)。
        新 IP への接続が張れないときは、旧 IP 経由の現在の接続から
        遅延ジョブで削除を仕掛ける(Issue #71 のフォールバック)。
        """
        iface = self.settings["iface"]
        cidr = self.settings["address_cidr"]
        sess = self._connect_new(new_ip)
        if sess is None:
            logger.warning("掃除用の新 IP 接続に失敗。旧接続からの遅延掃除に切替")
            return netadmin.schedule_stale_cleanup(self.session, iface, cidr)
        try:
            sess._hashi_sudo_pw = self.sudo_pw
            return netadmin.cleanup_addresses(sess, iface, cidr)
        finally:
            sess.close()

    def run(self):
        try:
            self.session._hashi_sudo_pw = self.sudo_pw
            res = netadmin.apply_static_ip(
                self.session,
                iface=self.settings["iface"],
                address_cidr=self.settings["address_cidr"],
                gateway=self.settings["gateway"],
                nameservers=self.settings["nameservers"],
                rollback_sec=self.settings["rollback_sec"],
                verify_reachable=self._verify_reachable,
                post_confirm=self._post_confirm,
            )
            self.ok.emit(res)
        except netadmin.NetAdminError as e:
            self.fail.emit(str(e))
        except Exception as e:  # noqa: BLE001
            logger.warning("静的 IP 設定で予期しない例外", exc_info=True)
            self.fail.emit(f"予期しないエラー: {e}")


class _P2PWorkerBase(QThread):
    """P2P 送受信の共通土台。SAS 照合は GUI へ問い合わせてブロック待機する。"""

    ask_confirm = Signal(str)      # SAS を渡して照合を依頼
    fail = Signal(str)

    def __init__(self):
        super().__init__()
        self._evt = threading.Event()
        self._confirmed = False

    def confirm(self, ok: bool):
        self._confirmed = ok
        self._evt.set()

    def _wait_confirm(self, sas: str) -> bool:
        self._evt.clear()
        self.ask_confirm.emit(sas)
        self._evt.wait()
        return self._confirmed


class P2PSendWorker(_P2PWorkerBase):
    """P2P でバンドルを送信する。"""

    ok = Signal()

    def __init__(self, host, port, payload):
        super().__init__()
        self.host, self.port, self.payload = host, port, payload

    def run(self):
        sess = None
        try:
            sock = p2p.connect(self.host, self.port)
            sess = p2p.Session(sock)
            sas = sess.handshake()
            if not self._wait_confirm(sas):
                self.fail.emit("")   # 中止(静かに終了)
                return
            sess.send_payload(self.payload)
            self.ok.emit()
        except p2p.P2PError as e:
            self.fail.emit(str(e))
        except Exception as e:  # noqa: BLE001
            logger.warning("P2P 送信で予期しない例外", exc_info=True)
            self.fail.emit(f"予期しないエラー: {e}")
        finally:
            if sess is not None:
                sess.close()


class P2PReceiveWorker(_P2PWorkerBase):
    """P2P でバンドルを受信する(受信の瞬間だけリッスンする)。"""

    ok = Signal(bytes)

    def __init__(self, host, port, accept_timeout=120.0):
        super().__init__()
        self.host, self.port, self.accept_timeout = host, port, accept_timeout

    def run(self):
        srv = sess = None
        try:
            srv = p2p.listen(self.host, self.port, timeout=self.accept_timeout)
            try:
                conn, _addr = srv.accept()
            except OSError as e:
                raise p2p.P2PError(
                    f"送信側からの接続待ちがタイムアウトしました ({e})") from e
            sess = p2p.Session(conn)
            sas = sess.handshake()
            if not self._wait_confirm(sas):
                self.fail.emit("")
                return
            payload = sess.receive_payload()
            self.ok.emit(payload)
        except p2p.P2PError as e:
            self.fail.emit(str(e))
        except Exception as e:  # noqa: BLE001
            logger.warning("P2P 受信で予期しない例外", exc_info=True)
            self.fail.emit(f"予期しないエラー: {e}")
        finally:
            if sess is not None:
                sess.close()
            if srv is not None:
                try:
                    srv.close()
                except OSError:
                    logger.debug("P2P リッスンソケットのクローズに失敗", exc_info=True)


class CloudSyncWorker(QThread):
    """クラウド同期(アップロード/ダウンロード)を GUI スレッド外で実行する。

    ネットワーク・OAuth はブロッキングなので必ずワーカーで動かす。渡された
    callable を呼び、結果 or 例外メッセージを Signal で返すだけの汎用ワーカー。
    """

    ok = Signal(object)
    fail = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        from .cloudsync import CloudSyncError
        try:
            self.ok.emit(self.fn())
        except CloudSyncError as e:
            self.fail.emit(str(e))
        except Exception as e:  # noqa: BLE001
            logger.warning("クラウド同期で予期しない例外", exc_info=True)
            self.fail.emit(f"予期しないエラー: {e}")


class SecretContext:
    """1 接続分の秘密情報を集約(sudo 提供・パスワード自動入力の供給源)。"""

    def __init__(self, profile, credentials, settings, parent_widget):
        self.profile = profile
        self.credentials = credentials
        self.settings = settings
        self.parent = parent_widget
        self._login_pw = None
        self._sudo_pw = None

    def note_login_password(self, pw):
        self._login_pw = pw

    def get_login_password(self):
        if self._login_pw:
            return self._login_pw
        if self.credentials:
            return self.credentials.get(self.profile, "password")
        return None

    def get_sudo_password(self, allow_prompt=True):
        if self._sudo_pw:
            return self._sudo_pw
        pw = self.credentials.get(self.profile, "sudo") if self.credentials else None
        if not pw and self.profile.sudo_same_as_password:
            pw = self.get_login_password()
        if not pw and allow_prompt:
            can_save = bool(self.credentials and self.credentials.available)
            secret, save = SecretDialog.ask(
                self.parent, f"{self.profile.username} の sudo パスワード",
                self.profile.save_secrets, can_save)
            if secret is None:
                return None
            pw = secret
            if save and self.credentials:
                self.credentials.set(self.profile, "sudo", pw)
        self._sudo_pw = pw
        return pw


class ConnectingWidget(QWidget):
    """接続処理中と結果を表示するページ。"""

    def __init__(self, profile: Profile, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignCenter)
        root.setSpacing(12)

        self.message = QLabel(f"{profile.label()} に接続しています…")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setStyleSheet("font-size:16px;")
        root.addWidget(self.message)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(280)
        root.addWidget(self.progress, 0, Qt.AlignCenter)

    def show_error(self, message: str):
        if message:
            text = f"接続に失敗しました:\n{message}"
        else:
            text = "接続を中止しました"
        self.message.setText(text)
        self.message.setStyleSheet("color:#e06c75; font-size:16px;")
        self.progress.hide()


class SessionTab(QWidget):
    """1 接続分のタブ。ターミナルと SFTP ブラウザを横並びで持つ。"""

    def __init__(self, session: SshSession, settings: Settings,
                 secret_ctx: SecretContext, parent=None, mode: str = "both"):
        super().__init__(parent)
        self.session = session
        self.settings = settings
        self.secret_ctx = secret_ctx
        self.mode = mode                    # "both" / "ssh" / "sftp"(Issue #112)
        self._use_terminal = mode in ("both", "ssh")
        self._use_browser = mode in ("both", "sftp")
        self.terminal = None
        self.browser = None
        self.session_log = None
        self.tunnels: list[Forward] = []
        self._last_autofill_ts = 0.0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 2)
        self.bt_term = QToolButton()
        self.bt_term.setText("ターミナル")
        self.bt_term.setCheckable(True)
        self.bt_term.setChecked(True)
        self.bt_files = QToolButton()
        self.bt_files.setText("ファイル")
        self.bt_files.setCheckable(True)
        self.bt_files.setChecked(True)
        # モードで無効になる側のトグルは押せないようにする(#112)
        self.bt_term.setEnabled(self._use_terminal)
        self.bt_files.setEnabled(self._use_browser)
        self.bt_term.setChecked(self._use_terminal)
        self.bt_files.setChecked(self._use_browser)
        # パスワード送信ボタン(Issue #40)。右クリック=貼り付けのため
        # 「右クリック→送信」は Shift が要ることが伝わらず使えなかった。
        # いつでも押せる常設ボタンにする(送る判断は常に人間、は維持)。
        self.bt_sendpw = QToolButton()
        self.bt_sendpw.setText("🔑 パスワード送信")
        self.bt_sendpw.setToolTip(
            "保存済みの sudo / ログインパスワードをターミナルへ送信します\n"
            "(Shift+右クリックのメニューからも送信できます)")
        self.bt_sendpw.clicked.connect(
            lambda: self._on_password_prompt("manual"))
        info = QLabel(f"{session.profile.username}@{session.profile.host}:{session.profile.port}")
        info.setStyleSheet(f"color:{style.FG_MUTED};")
        bar.addWidget(self.bt_term)
        bar.addWidget(self.bt_files)
        bar.addWidget(self.bt_sendpw)
        bar.addStretch(1)
        bar.addWidget(info)
        root.addLayout(bar)

        self.splitter = QSplitter(Qt.Horizontal)
        self._logging_enabled_at_start = False
        if self._use_terminal:
            self.terminal = TerminalWidget(
                font_size=settings.get("terminal_font_size"),
                right_click_paste=settings.get("right_click_paste"),
                theme=settings.get("terminal_theme") or "",
                font_family=settings.get("terminal_font_family") or "",
            )
            # セッションログ (Issue #85)。設定が OFF でもインスタンスは持ち、
            # メニューから後から開始できるようにしておく。
            self.session_log = SessionLog(
                session.profile.label() or session.profile.id_str(),
                settings.get("session_log_dir") or None,
                enabled=bool(settings.get("session_log")),
            )
            self.terminal.set_session_log(self.session_log)
            self._logging_enabled_at_start = self.session_log.is_open()
            self.splitter.addWidget(self.terminal)
        if self._use_browser:
            self.browser = SftpBrowser(
                session, session.profile.initial_path,
                settings=settings,
                sudo_provider=lambda: self.secret_ctx.get_sudo_password(),
                sudo_provider_silent=lambda: self.secret_ctx.get_sudo_password(
                    allow_prompt=False),
                parent=self,
            )
            self.splitter.addWidget(self.browser)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        root.addWidget(self.splitter, 1)

        self.bt_term.toggled.connect(self._apply_visibility)
        self.bt_files.toggled.connect(self._apply_visibility)
        if self._use_terminal and self._use_browser:
            self.browser.terminal_input.connect(self.terminal.send_text)
        if self._use_terminal:
            self.terminal.password_prompt.connect(self._on_password_prompt)
        # SFTP のみのモードではパスワード送信ボタンは意味がない
        self.bt_sendpw.setEnabled(self._use_terminal)

        # トースト(通知)
        self._toast = QLabel(self)
        self._toast.setStyleSheet(
            f"background:{style.BG_RAISED}; color:{style.FG};"
            f" border:1px solid {style.BORDER};"
            f" border-radius:{style.TOAST_RADIUS}px; padding:6px 12px;")
        self._toast.setVisible(False)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self._toast.setVisible(False))

        # sudo ワンタップ送信ボタン。リモートはプロンプトを偽装できるため
        # 自動送信はしない(送る判断は常に人間)。ただしワンタップで済むようにする。
        self._sudo_btn = QPushButton("🔑 sudo パスワードを送信", self)
        self._sudo_btn.setStyleSheet(
            f"QPushButton {{ background:{style.OK}; color:#12200f;"
            f" border:none; border-radius:{style.TOAST_RADIUS}px;"
            f" padding:6px 14px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:{style.ACCENT_HOVER}; color:#fff; }}")
        self._sudo_btn.setCursor(Qt.PointingHandCursor)
        self._sudo_btn.setVisible(False)
        self._sudo_btn.clicked.connect(self._send_sudo_password)
        self._sudo_btn_timer = QTimer(self)
        self._sudo_btn_timer.setSingleShot(True)
        self._sudo_btn_timer.timeout.connect(lambda: self._sudo_btn.setVisible(False))

        # シェル開始(SSH モードのみ)
        if self._use_terminal:
            ch = session.open_shell()
            self.terminal.attach(ch)
            self.terminal.setFocus()

    # ---- パスワード自動入力 -------------------------------------------------
    def _on_password_prompt(self, kind: str):
        import time
        if self.terminal is None:
            return
        if kind == "manual":
            pw = (self.secret_ctx.get_sudo_password()
                  or self.secret_ctx.get_login_password())
            if pw:
                self.terminal.send_password(pw)
                self._flash("🔑 保存したパスワードを送信しました")
            else:
                self._flash("送信できるパスワードがありません", warn=True)
            return
        if kind == "sudo":
            if not self.settings.get("sudo_autofill"):
                self._flash("sudo パスワード要求: 上の 🔑 ボタンで送信できます")
                return
            now = time.monotonic()
            if now - self._last_autofill_ts < 8.0:
                # 送信直後の再要求 = おそらく誤り。同じものを再送しても無駄なので
                # ボタンは出さず手動に委ねる
                self._flash("パスワードが違うようです。手動で入力してください", warn=True)
                return
            self._show_sudo_button()
        elif kind in ("password", "passphrase"):
            # 別ホストの可能性があるので自動送信しない(手動送信は可能)
            self._flash("パスワード要求: 上の 🔑 ボタンで保存済みを送れます")

    def _show_sudo_button(self):
        self._sudo_btn.adjustSize()
        self._sudo_btn.move(
            max(12, (self.width() - self._sudo_btn.width()) // 2), 40)
        self._sudo_btn.setVisible(True)
        self._sudo_btn.raise_()
        self._sudo_btn_timer.start(20000)  # プロンプトが流れた頃に自動で消す

    def _send_sudo_password(self):
        import time
        self._sudo_btn.setVisible(False)
        self._sudo_btn_timer.stop()
        pw = self.secret_ctx.get_sudo_password()
        if pw:
            self._last_autofill_ts = time.monotonic()
            self.terminal.send_password(pw)
            self._flash("🔑 sudo パスワードを送信しました")
        else:
            self._flash("送信できる sudo パスワードがありません", warn=True)

    def _flash(self, text: str, warn: bool = False):
        self._toast.setText(text)
        self._toast.setStyleSheet(
            "background:%s; color:#fff; border-radius:%dpx; padding:6px 12px;"
            % (style.DANGER_BG if warn else style.BG_RAISED, style.TOAST_RADIUS))
        self._toast.adjustSize()
        self._toast.move(max(12, (self.width() - self._toast.width()) // 2), 40)
        self._toast.setVisible(True)
        self._toast.raise_()
        self._toast_timer.start(3500)

    # ---- ポートフォワード ----------------------------------------------------
    def add_tunnel(self, spec: dict) -> str:
        kind = spec.get("type", "local")
        if kind == "local":
            fw = LocalForward(
                self.session.transport, spec["local_host"], spec["local_port"],
                spec["remote_host"], spec["remote_port"])
        elif kind == "remote":
            fw = RemoteForward(
                self.session.transport, spec["remote_host"], spec["remote_port"],
                spec["local_host"], spec["local_port"])
        elif kind == "dynamic":
            fw = DynamicForward(
                self.session.transport, spec["local_host"], spec["local_port"])
        else:
            raise ValueError(f"未知のフォワード種別: {kind}")
        fw.start()
        self.tunnels.append(fw)
        return fw.label()

    def _apply_visibility(self):
        if not self.bt_term.isChecked() and not self.bt_files.isChecked():
            sender = self.sender()
            other = self.bt_files if sender is self.bt_term else self.bt_term
            if other.isEnabled():
                other.setChecked(True)
        if self.terminal is not None:
            self.terminal.setVisible(self.bt_term.isChecked())
        if self.browser is not None:
            self.browser.setVisible(self.bt_files.isChecked())

    def toggle_session_log(self) -> bool:
        """セッションログを開始/停止し、開始状態を返す。"""
        if self.terminal is None:
            return False   # SFTP のみのモードにはターミナル出力がない
        if self.session_log.is_open():
            try:
                self.session_log.flush_visible(self.terminal.screen)
            except Exception:
                logger.debug("セッションログ停止前の flush に失敗", exc_info=True)
            self.session_log.close()
            self.terminal.set_session_log(None)
            self.session_log = SessionLog(
                self.session.profile.label() or self.session.profile.id_str(),
                self.settings.get("session_log_dir") or None,
                enabled=False,
            )
            return False
        # 停止中なら新しいファイルを開いて開始
        self.session_log = SessionLog(
            self.session.profile.label() or self.session.profile.id_str(),
            self.settings.get("session_log_dir") or None,
            enabled=True,
        )
        self.terminal.set_session_log(self.session_log)
        return True

    def shutdown(self):
        for fw in self.tunnels:
            fw.stop()
        if self.browser is not None:
            self.browser.shutdown()
        if self.terminal is not None:
            self.terminal.detach()
        self.session.close()

    def reconnect_session(self, session: SshSession):
        """接続を張り直し、ターミナル/ブラウザを新しい session に乗り換える。"""
        old_session = self.session
        self.session = session
        if self.terminal is not None:
            try:
                self.terminal.detach()
                ch = session.open_shell(
                    cols=self.terminal.screen.columns,
                    rows=self.terminal.screen.lines,
                )
                self.terminal.attach(ch)
            except Exception:  # noqa: BLE001
                logger.warning("ターミナル再接続に失敗", exc_info=True)
        if self.browser is not None:
            try:
                self.browser.reconnect_session(session)
            except Exception:  # noqa: BLE001
                logger.warning("SFTP ブラウザ再接続に失敗", exc_info=True)
        for fw in self.tunnels:
            fw.stop()
        self.tunnels = []
        try:
            old_session.close()
        except Exception:
            logger.debug("古い session の close に失敗 (無視)", exc_info=True)


class _SharedOps:
    """ランチャーとセッションウィンドウで共通のメニュー操作。

    self.store / known_hosts / credentials / settings と、鍵生成/P2P 用の
    worker リストを前提にする。プロファイル一覧の再描画は各クラスが
    _refresh_profile_lists() で実装する。
    """

    # ---- 書き出し / 読み込み (Issue #42) -------------------------------------
    def _export_profiles(self):
        if not self.store.profiles:
            QMessageBox.information(self, "書き出し",
                                    "書き出すプロファイルがありません。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "接続情報を書き出す", "hashi-profiles.json",
            "Hashi エクスポート (*.json)")
        if not path:
            return
        passphrase = None
        if self.credentials.available:
            r = QMessageBox.question(
                self, "秘密情報の扱い",
                "保存済みのパスワード / パスフレーズ / sudo パスワードも"
                "含めますか?\n(含める場合はパスフレーズで暗号化します。"
                "平文では書き出しません)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r == QMessageBox.Yes:
                passphrase = self._ask_new_export_passphrase()
                if passphrase is None:
                    return
        try:
            counts = portability.export_bundle(
                path, self.store.profiles, self.known_hosts,
                self.credentials, passphrase)
        except portability.PortabilityError as e:
            QMessageBox.warning(self, "書き出し", str(e))
            return
        msg = f"{counts['profiles']} 件のプロファイルを書き出しました。"
        if passphrase:
            msg += f"\n(暗号化した秘密情報 {counts['secrets']} 件を含む)"
        else:
            msg += "\n(パスワード等の秘密情報は含まれていません)"
        QMessageBox.information(self, "書き出し", msg)

    def _ask_new_export_passphrase(self) -> str | None:
        while True:
            p1 = ask_secret(self, "エクスポート用のパスフレーズを入力")
            if p1 is None:
                return None
            if not p1:
                QMessageBox.warning(self, "書き出し",
                                    "パスフレーズを空にはできません。")
                continue
            p2 = ask_secret(self, "確認のためもう一度入力")
            if p2 is None:
                return None
            if p1 == p2:
                return p1
            QMessageBox.warning(self, "書き出し", "パスフレーズが一致しません。")

    def _import_profiles(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "接続情報を読み込む", "",
            "Hashi エクスポート (*.json);;すべてのファイル (*)")
        if not path:
            return
        try:
            bundle = portability.load_bundle(path)
        except portability.PortabilityError as e:
            QMessageBox.warning(self, "読み込み", str(e))
            return
        self._apply_imported_bundle(bundle)

    def _apply_imported_bundle(self, bundle, title: str = "読み込み"):
        """読み込んだバンドルを対話的に統合する(ファイル/P2P 受信で共用)。"""
        if bundle.has_encrypted_secrets and self.credentials.available:
            for _ in range(3):
                pw = ask_secret(
                    self, "エクスポート時のパスフレーズを入力\n"
                    "(キャンセルすると秘密情報を除いて読み込みます)")
                if pw is None:
                    break
                try:
                    bundle.decrypt_secrets(pw)
                    break
                except portability.PortabilityError as e:
                    QMessageBox.warning(self, title, str(e))
        overwrite = False
        ids = {p.id_str() for p in self.store.profiles}
        dups = sum(1 for p in bundle.profiles if p.id_str() in ids)
        if dups:
            r = QMessageBox.question(
                self, title,
                f"既存と重複するプロファイルが {dups} 件あります。"
                "上書きしますか?\n(いいえ = 既存を残してスキップ)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            overwrite = r == QMessageBox.Yes
        counts = portability.merge_bundle(
            bundle, self.store, self.known_hosts, self.credentials, overwrite)
        self._refresh_profile_lists()
        msg = (f"追加 {counts['added']} 件 / 上書き {counts['updated']} 件 / "
               f"スキップ {counts['skipped']} 件\n"
               f"ホスト鍵の記録を {counts['hosts_added']} 件追加"
               "(既存の記録は上書きしません)")
        if counts["secrets"]:
            msg += f"\n秘密情報 {counts['secrets']} 件を保存しました"
        QMessageBox.information(self, title, msg)

    # ---- P2P 共有 (Issue #43) -----------------------------------------------
    def _p2p_send(self):
        if not self.store.profiles:
            QMessageBox.information(self, "P2P 送信",
                                    "送信するプロファイルがありません。")
            return
        dlg = P2PSendDialog(self, default_port=p2p.DEFAULT_PORT)
        if not dlg.exec():
            return
        target = dlg.result_target()
        passphrase = None
        if target["include_secrets"] and self.credentials.available:
            passphrase = self._ask_new_export_passphrase()
            if passphrase is None:
                return
        payload = portability.dumps_bundle(
            self.store.profiles, self.known_hosts,
            self.credentials if passphrase else None, passphrase)
        worker = P2PSendWorker(target["host"], target["port"], payload)
        worker.ask_confirm.connect(
            lambda sas, w=worker: w.confirm(
                SasConfirmDialog.confirm(self, sas, "送信")))
        worker.ok.connect(
            lambda: QMessageBox.information(
                self, "P2P 送信", "接続情報を送信しました。"))
        worker.fail.connect(self._on_p2p_fail)
        worker.finished.connect(lambda w=worker: self._p2p_workers.remove(w))
        self._p2p_workers.append(worker)
        self.statusBar().showMessage("P2P: 相手に接続しています…")
        worker.start()

    def _p2p_receive(self):
        r = QMessageBox.question(
            self, "P2P 受信",
            f"ポート {p2p.DEFAULT_PORT} で送信側からの接続を待ちます"
            "(最大 2 分)。続行しますか?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if r != QMessageBox.Yes:
            return
        worker = P2PReceiveWorker("0.0.0.0", p2p.DEFAULT_PORT)
        worker.ask_confirm.connect(
            lambda sas, w=worker: w.confirm(
                SasConfirmDialog.confirm(self, sas, "受信")))
        worker.ok.connect(self._on_p2p_received)
        worker.fail.connect(self._on_p2p_fail)
        worker.finished.connect(lambda w=worker: self._p2p_workers.remove(w))
        self._p2p_workers.append(worker)
        self.statusBar().showMessage(
            f"P2P: ポート {p2p.DEFAULT_PORT} で受信待ち…")
        worker.start()

    def _on_p2p_received(self, payload: bytes):
        try:
            bundle = portability.loads_bundle(payload)
        except portability.PortabilityError as e:
            QMessageBox.warning(self, "P2P 受信", str(e))
            return
        self._apply_imported_bundle(bundle, title="P2P 受信")

    def _on_p2p_fail(self, msg: str):
        if msg:
            QMessageBox.warning(self, "P2P", msg)
        self.statusBar().showMessage(
            "P2P: 失敗しました" if msg else "P2P: 中止しました", 4000)

    # ---- クラウド同期 (Issue #44) -------------------------------------------
    def _cloud_backend(self):
        """同期先 backend を作る。今は Google Drive のみ。"""
        from .cloudsync import GoogleDriveBackend
        return GoogleDriveBackend()

    def _run_cloud(self, fn, on_ok, busy_msg):
        worker = CloudSyncWorker(fn)
        worker.ok.connect(on_ok)
        worker.fail.connect(
            lambda msg: QMessageBox.warning(self, "クラウド同期", msg))
        worker.finished.connect(lambda w=worker: self._cloud_workers.remove(w))
        self._cloud_workers.append(worker)
        self.statusBar().showMessage(busy_msg)
        worker.start()

    def _cloud_upload(self):
        from . import cloudsync
        if not self.store.profiles:
            QMessageBox.information(self, "クラウド同期",
                                    "アップロードするプロファイルがありません。")
            return
        master = self._ask_new_export_passphrase()   # 2 回入力で確認
        if master is None:
            return
        secrets_pp = None
        if self.credentials.available:
            r = QMessageBox.question(
                self, "クラウド同期",
                "保存済みの秘密情報も同期しますか?\n"
                "(含める場合は別のパスフレーズで暗号化します)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r == QMessageBox.Yes:
                secrets_pp = self._ask_new_export_passphrase()
                if secrets_pp is None:
                    return
        backend = self._cloud_backend()
        profiles = list(self.store.profiles)
        kh, creds = self.known_hosts, self.credentials

        def job():
            return cloudsync.push(backend, profiles, kh, master,
                                  creds if secrets_pp else None, secrets_pp)

        self._run_cloud(
            job,
            lambda res: QMessageBox.information(
                self, "クラウド同期",
                f"{res['profiles']} 件をアップロードしました"
                + (f"(秘密情報 {res['secrets']} 件含む)"
                   if res["secrets"] else "")),
            "クラウドへアップロードしています…")

    def _cloud_download(self):
        from . import cloudsync
        master = ask_secret(self, "クラウド同期のマスターパスフレーズを入力")
        if not master:
            return
        secrets_pp = None
        if self.credentials.available:
            r = QMessageBox.question(
                self, "クラウド同期",
                "同期データに秘密情報が含まれていれば取り込みますか?\n"
                "(取り込む場合は暗号化に使ったパスフレーズが必要)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r == QMessageBox.Yes:
                secrets_pp = ask_secret(self, "秘密情報のパスフレーズを入力")
        backend = self._cloud_backend()
        store, kh, creds = self.store, self.known_hosts, self.credentials

        def job():
            return cloudsync.pull_and_merge(
                backend, master, store, kh,
                creds if secrets_pp else None, secrets_pp, overwrite=True)

        self._run_cloud(job, self._on_cloud_downloaded,
                        "クラウドからダウンロードしています…")

    def _on_cloud_downloaded(self, counts: dict):
        if counts.get("empty"):
            QMessageBox.information(self, "クラウド同期",
                                    "クラウドに同期データがありませんでした。")
            return
        self._refresh_profile_lists()
        msg = (f"追加 {counts['added']} 件 / 上書き {counts['updated']} 件 / "
               f"スキップ {counts['skipped']} 件\n"
               f"ホスト鍵 {counts['hosts_added']} 件を追加")
        if counts.get("secrets"):
            msg += f"\n秘密情報 {counts['secrets']} 件を保存"
        if counts.get("backup"):
            msg += f"\n取り込み前の手元をバックアップ: {counts['backup']}"
        QMessageBox.information(self, "クラウド同期", msg)

    # ---- 鍵生成 (Issue #12) -------------------------------------------------
    def _generate_key(self):
        tab = getattr(self, "session_tab", None)
        dlg = KeygenDialog(self, can_register=tab is not None)
        if not dlg.exec():
            return
        worker = KeygenWorker(dlg.result_settings(), tab.session if tab else None)
        worker.ok.connect(self._on_keygen_ok)
        worker.fail.connect(
            lambda message: QMessageBox.warning(self, "SSH 鍵の生成", message)
        )
        worker.finished.connect(lambda w=worker: self._keygen_workers.remove(w))
        self._keygen_workers.append(worker)
        self.statusBar().showMessage("SSH 鍵を生成しています…")
        worker.start()

    def _on_keygen_ok(self, path: str, registered: bool):
        message = f"秘密鍵を保存しました:\n{path}"
        if registered:
            message += "\n公開鍵を接続先の authorized_keys に登録しました。"
        self.statusBar().showMessage(
            "SSH 鍵を生成しました" + ("（公開鍵を登録しました）" if registered else ""),
            5000,
        )
        QMessageBox.information(self, "SSH 鍵の生成", message)

    def _open_settings(self):
        if SettingsDialog(self.settings, self).exec():
            self._apply_ui_settings_live()

    def _apply_ui_settings_live(self):
        """テーマ / フォント設定を開いている全セッションへ即時反映(#99)。

        以前は「新しい接続から反映」だったが、再接続が必要で面倒という
        オーナーのフィードバックにより、開いているターミナルにも適用する。
        """
        theme = self.settings.get("terminal_theme") or ""
        family = self.settings.get("terminal_font_family") or ""
        size = self.settings.get("terminal_font_size")
        for win in SessionWindow._windows:
            tab = win.session_tab
            if tab is None or tab.terminal is None:
                continue
            try:
                tab.terminal.set_theme(theme)
                tab.terminal.set_font_family(family)
                tab.terminal.set_font_size(size)
            except RuntimeError:
                logger.debug("閉じられたウィンドウへの設定反映をスキップ")

    def _about(self):
        QMessageBox.information(
            self, "Hashi について",
            f"Hashi v{APP_VERSION}\n\n"
            "SSH ターミナル + SFTP ファイルブラウザ\n"
            "橋 (bridge) — ローカルとリモートをつなぐ。\n\n"
            "Python / PySide6 / paramiko / pyte",
        )


def _relative_time(ts: float) -> str:
    """UNIX 秒 → 「3 日前」のような相対表示(#81)。"""
    if not ts:
        return ""
    delta = time.time() - ts
    if delta < 60:
        return "たった今"
    if delta < 3600:
        return f"{int(delta // 60)} 分前"
    if delta < 86400:
        return f"{int(delta // 3600)} 時間前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 日前"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _launcher_order(profiles, query: str = ""):
    """(store 上の index, Profile) を検索で絞り、最終接続が新しい順に返す(#81)。

    一覧はソート/フィルタで並びが変わるため、行番号ではなくこの index を
    QListWidgetItem に持たせて編集/削除の対象を特定する。
    """
    q = query.strip().lower()
    result = []
    for i, p in enumerate(profiles):
        haystack = " ".join(
            [p.name, p.host, p.username, " ".join(p.tags)]).lower()
        if q and q not in haystack:
            continue
        result.append((i, p))
    result.sort(key=lambda t: (-t[1].last_connected, t[1].label().lower()))
    return result


class LauncherWindow(_SharedOps, QMainWindow):
    """起動時のサーバー選択ランチャー(Issue #14 段階2)。

    接続すると常に独立した SessionWindow を開く。タブは持たない。
    ストア類(プロファイル/既知ホスト/認証情報/設定)は全ウィンドウで共有する。
    """

    _instance: "LauncherWindow | None" = None

    def __init__(self, services: dict | None = None):
        super().__init__()
        self.setWindowTitle(f"Hashi — 接続先を選択 v{APP_VERSION}")
        fit_to_screen(self, 420, 640)

        if services is None:
            services = {
                "store": ProfileStore(),
                "known_hosts": KnownHosts(),
                "settings": Settings(),
                "credentials": CredentialStore(),
                "snippets": SnippetStore(),
            }
        self._services = services
        self.snippets = services["snippets"]
        self.store = services["store"]
        self.known_hosts = services["known_hosts"]
        self.settings = services["settings"]
        self.credentials = services["credentials"]
        self._keygen_workers: list[KeygenWorker] = []
        self._p2p_workers: list[_P2PWorkerBase] = []
        self._cloud_workers: list[CloudSyncWorker] = []
        LauncherWindow._instance = self

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(8)
        self._update_banner = self._create_update_banner()
        v.addWidget(self._update_banner)
        title = QLabel("接続先を選択")
        title.setStyleSheet("font-size:16px; font-weight:bold;")
        btn_new = QPushButton("＋ 新しい接続")
        btn_new.clicked.connect(self.new_profile)
        # インクリメンタル検索(#81)。名前/ホスト/ユーザー/タグで絞り込み
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("🔍 検索 (名前 / ホスト / ユーザー / タグ)")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(self._reload_list)
        self.list = QListWidget()
        self.list.setSpacing(2)
        self.list.itemDoubleClicked.connect(self._connect_item)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._profile_menu)
        hint = QLabel("ダブルクリックで接続(新しいウィンドウが開きます)\n"
                      "右クリックで編集/削除")
        hint.setStyleSheet(f"color:{style.FG_MUTED};")
        v.addWidget(title)
        v.addWidget(btn_new)
        v.addWidget(self.ed_search)
        v.addWidget(self.list, 1)
        v.addWidget(hint)
        self.setCentralWidget(central)

        self._build_menu()
        self._reload_list()
        self.statusBar().showMessage("準備完了")

        if self.settings.get("update_check"):
            self._update_worker = UpdateCheckWorker(parent=self)
            self._update_worker.new_version.connect(self._show_update_banner)
            self._update_worker.start()

    def _create_update_banner(self):
        """新バージョン通知バナーを生成する。"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(style.SPACING)
        self._update_label = QLabel()
        self._update_label.setWordWrap(True)
        self._update_label.setOpenExternalLinks(True)
        self._update_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        close = QPushButton("×")
        close.setFlat(True)
        close.setToolTip("閉じる")
        close.clicked.connect(lambda: w.setVisible(False))
        h.addWidget(self._update_label, 1)
        h.addWidget(close)
        w.setStyleSheet(
            f"background-color:{style.ACCENT}; color:{style.FG}; "
            f"border-radius:{style.TOAST_RADIUS}px;"
        )
        w.setVisible(False)
        return w

    def _show_update_banner(self, version: str, url: str):
        self._update_label.setText(
            f"新しいバージョン {version} が利用可能です — "
            f"<a style='color:{style.FG};' href='{url}'>ダウンロード</a>"
        )
        self._update_banner.setVisible(True)

    def _build_menu(self):
        m_file = self.menuBar().addMenu("ファイル")
        m_file.addAction("新しい接続…", self.new_profile)
        m_file.addAction("SSH 鍵を生成…", self._generate_key)
        m_file.addSeparator()
        m_file.addAction("接続情報を書き出す…", self._export_profiles)
        m_file.addAction("接続情報を読み込む…", self._import_profiles)
        m_file.addSeparator()
        m_file.addAction("接続情報を送信 (P2P)…", self._p2p_send)
        m_file.addAction("接続情報を受信 (P2P)…", self._p2p_receive)
        m_file.addSeparator()
        m_file.addAction("クラウドへアップロード…", self._cloud_upload)
        m_file.addAction("クラウドからダウンロード…", self._cloud_download)
        m_file.addSeparator()
        m_file.addAction("設定…", self._open_settings)
        m_file.addSeparator()
        m_file.addAction("終了", self.close)

        m_help = self.menuBar().addMenu("ヘルプ")
        m_help.addAction("Hashi について", self._about)

    def _refresh_profile_lists(self):
        self._reload_list()

    def _reload_list(self):
        self.list.clear()
        query = (self.ed_search.text() if hasattr(self, "ed_search") else "")
        for idx, p in _launcher_order(self.store.profiles, query):
            second = f"{p.username}@{p.host}:{p.port}"
            if p.tags:
                second += "   🏷 " + ", ".join(p.tags)
            if p.last_connected:
                second += f"   ⏱ {_relative_time(p.last_connected)}"
            item = QListWidgetItem(f"{p.label()}\n{second}")
            item.setIcon(style.color_dot_icon(p.color))
            item.setData(Qt.UserRole, idx)   # 検索/ソートでズレない実 index
            item.setToolTip(f"{p.username}@{p.host}:{p.port} ({p.auth_method})")
            self.list.addItem(item)

    def _selected_store_index(self) -> int:
        """選択中アイテムの store.profiles 上の index(-1 = 未選択)。"""
        item = self.list.currentItem()
        if item is None:
            return -1
        idx = item.data(Qt.UserRole)
        return idx if isinstance(idx, int) and 0 <= idx < len(self.store.profiles) else -1

    def new_profile(self):
        dlg = ConnectDialog(self, credentials=self.credentials)
        if dlg.exec():
            profile = dlg.result_profile()
            self.store.add(profile)
            dlg.apply_credentials(profile)
            self._reload_list()

    def _profile_menu(self, pos):
        row = self._selected_store_index()
        if row < 0:
            return
        menu = QMenu(self)
        a_conn = menu.addAction("接続(ターミナル + ファイル)")
        a_ssh = menu.addAction("ターミナルのみで接続")
        a_sftp = menu.addAction("ファイルのみで接続")
        menu.addSeparator()
        a_edit = menu.addAction("編集…")
        a_del = menu.addAction("削除")
        chosen = menu.exec(self.list.viewport().mapToGlobal(pos))
        if chosen is a_conn:
            self._connect(self.store.profiles[row])
        elif chosen is a_ssh:
            self._connect(self.store.profiles[row], mode="ssh")
        elif chosen is a_sftp:
            self._connect(self.store.profiles[row], mode="sftp")
        elif chosen is a_edit:
            dlg = ConnectDialog(
                self, self.store.profiles[row], self.credentials)
            if dlg.exec():
                profile = dlg.result_profile()
                self.store.update(row, profile)
                dlg.apply_credentials(profile)
                self._reload_list()
        elif chosen is a_del:
            p = self.store.profiles[row]
            r = QMessageBox.question(
                self, "プロファイルの削除",
                f"「{p.label()}」を一覧から削除しますか?\n(サーバー側には何も影響しません)",
            )
            if r == QMessageBox.Yes:
                self.store.remove(row)
                self._reload_list()

    def _connect_item(self, item: QListWidgetItem):
        idx = item.data(Qt.UserRole)
        if isinstance(idx, int) and 0 <= idx < len(self.store.profiles):
            self._connect(self.store.profiles[idx])

    def _connect(self, profile: Profile, mode: str = "both"):
        """独立したウィンドウを開いて接続する(常に新ウィンドウ)。

        mode: "both"(ターミナル+ファイル)/ "ssh"(ターミナルのみ)/
              "sftp"(ファイルのみ)(Issue #112)。
        """
        win = SessionWindow(self._services, profile, launcher=self, mode=mode)
        win.show()
        win.start_connect()

    def closeEvent(self, ev):
        # ランチャーを閉じてもセッションウィンドウは残す(アプリは最後の
        # ウィンドウが閉じたときに終了する)。
        if LauncherWindow._instance is self:
            LauncherWindow._instance = None
        ev.accept()


class SessionWindow(_SharedOps, QMainWindow):
    """1 接続 = 1 ウィンドウ(Issue #14 段階2)。セッションメニューを内蔵する。"""

    _windows: list["SessionWindow"] = []

    def __init__(self, services: dict, profile: Profile, launcher=None,
                 mode: str = "both"):
        super().__init__()
        self._services = services
        self.store = services["store"]
        self.known_hosts = services["known_hosts"]
        self.settings = services["settings"]
        self.credentials = services["credentials"]
        self.snippets = services["snippets"]
        self._launcher = launcher
        self._mode = mode                   # both / ssh / sftp(Issue #112)
        self.profile = profile
        self.session_tab: SessionTab | None = None
        self._workers: list[ConnectWorker] = []
        self._keygen_workers: list[KeygenWorker] = []
        self._sshd_workers: list[SshdHardenWorker] = []
        self._netadmin_workers: list[NetAdminWorker] = []
        self._p2p_workers: list[_P2PWorkerBase] = []
        self._cloud_workers: list[CloudSyncWorker] = []
        SessionWindow._windows.append(self)

        fit_to_screen(self, 1280, 760)
        self.setWindowTitle(f"接続中: {profile.label()}")
        self._connecting = ConnectingWidget(profile)
        self.setCentralWidget(self._connecting)
        self._build_menu()
        self._build_reconnect_bar()
        self.statusBar().showMessage(f"{profile.label()} に接続中…")

        self._reconnect_attempts = 0
        self._reconnecting = False
        self._alive_timer = QTimer(self)
        self._alive_timer.setInterval(2000)
        self._alive_timer.timeout.connect(self._check_alive)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._start_reconnect)

    def _build_reconnect_bar(self):
        bar = QToolBar(self)
        bar.setMovable(False)
        bar.setFloatable(False)
        label = QLabel("接続が切断されました")
        label.setStyleSheet(f"color:{style.ERROR}; padding:2px 6px;")
        btn = QPushButton("再接続")
        btn.setToolTip("今すぐ再接続を試みます")
        btn.clicked.connect(self._manual_reconnect)
        bar.addWidget(label)
        bar.addWidget(btn)
        bar.setVisible(False)
        self.addToolBar(Qt.TopToolBarArea, bar)
        self._reconnect_bar = bar

    def _build_menu(self):
        m_file = self.menuBar().addMenu("ファイル")
        m_file.addAction("サーバー一覧を開く", self._open_launcher)
        m_file.addSeparator()
        m_file.addAction("接続情報を書き出す…", self._export_profiles)
        m_file.addAction("接続情報を読み込む…", self._import_profiles)
        m_file.addSeparator()
        m_file.addAction("クラウドへアップロード…", self._cloud_upload)
        m_file.addAction("クラウドからダウンロード…", self._cloud_download)
        m_file.addSeparator()
        m_file.addAction("設定…", self._open_settings)
        m_file.addSeparator()
        m_file.addAction("このウィンドウを閉じる", self.close)

        m_view = self.menuBar().addMenu("表示")
        act_plus = m_view.addAction("ターミナル文字を大きく")
        act_plus.setShortcut("Ctrl+=")
        act_plus.triggered.connect(lambda: self._font_delta(+1))
        act_minus = m_view.addAction("ターミナル文字を小さく")
        act_minus.setShortcut("Ctrl+-")
        act_minus.triggered.connect(lambda: self._font_delta(-1))

        self.m_sess = self.menuBar().addMenu("セッション")
        self.m_sess.addAction("ポートフォワードを追加…", self._add_tunnel)
        self.m_sess.addAction("ポートフォワード一覧…", self._list_tunnels)
        self.m_sess.addAction("SSH 鍵を生成…", self._generate_key)
        self.m_sess.addAction("SSH サーバー設定を変更…", self._harden_sshd)
        self.m_sess.addAction("サーバーの IP を固定…", self._static_ip)
        self.m_sess.addSeparator()
        self.act_session_log = self.m_sess.addAction("セッションログを開始")
        self.act_session_log.triggered.connect(self._toggle_session_log)
        self.m_sess.addSeparator()
        self.m_sess.addAction("この接続の保存パスワードを削除",
                              self._forget_credentials)
        self.m_sess.setEnabled(False)   # 接続完了までは無効

        self.m_snippets = self.menuBar().addMenu("スニペット")
        self.m_snippets.addAction("スニペットを管理…", self._manage_snippets)
        self.m_snippets.addSeparator()
        self.m_snippets.aboutToShow.connect(self._populate_snippets_menu)

        m_help = self.menuBar().addMenu("ヘルプ")
        m_help.addAction("Hashi について", self._about)

    def _manage_snippets(self):
        SnippetsManageDialog.manage(self, self.snippets)

    def _populate_snippets_menu(self):
        self.m_snippets.clear()
        self.m_snippets.addAction("スニペットを管理…", self._manage_snippets)
        self.m_snippets.addSeparator()
        if not self.snippets.snippets:
            act = self.m_snippets.addAction("スニペットがありません")
            act.setEnabled(False)
            return
        for snippet in self.snippets.snippets:
            act = self.m_snippets.addAction(snippet.name)
            act.triggered.connect(lambda checked, s=snippet: self._send_snippet(s))

    def _send_snippet(self, snippet: Snippet):
        if self.session_tab is None or self.session_tab.terminal is None:
            QMessageBox.information(
                self, "スニペット", "ターミナル接続中のみスニペットを送信できます。")
            return
        values = SnippetVariablesDialog.ask(self, snippet.body)
        if values is None:
            return
        body = expand_snippet(snippet.body, values)
        if snippet.send_enter:
            body += "\n"
        self.session_tab.terminal.send_text(body)

    def _refresh_profile_lists(self):
        if self._launcher is not None:
            self._launcher._reload_list()
        elif LauncherWindow._instance is not None:
            LauncherWindow._instance._reload_list()

    def _open_launcher(self):
        inst = LauncherWindow._instance
        if inst is None:
            inst = LauncherWindow(services=self._services)
        inst.show()
        inst.raise_()
        inst.activateWindow()

    # ---- 接続 --------------------------------------------------------------
    def start_connect(self):
        worker = ConnectWorker(self.profile, self.known_hosts, self.credentials,
                               self.settings)
        worker.ask_secret.connect(
            lambda prompt, ds, cs, w=worker:
            w.provide(SecretDialog.ask(self, prompt, ds, cs))
        )
        worker.ask_hostkey.connect(
            lambda info, w=worker: w.provide(HostKeyDialog.ask(self, info))
        )
        worker.ok.connect(lambda s, w=worker: self._on_connected(s, w))
        worker.fail.connect(self._on_connect_failed)
        worker.finished.connect(lambda w=worker: self._workers.remove(w))
        self._workers.append(worker)
        worker.start()

    def _on_connected(self, session: SshSession, worker: ConnectWorker):
        ctx = SecretContext(session.profile, self.credentials,
                            self.settings, self)
        if worker.used_password:
            ctx.note_login_password(worker.used_password)
        tab = SessionTab(session, self.settings, ctx, mode=self._mode)
        self.session_tab = tab
        if tab.terminal is not None:
            tab.terminal.set_snippet_store(self.snippets)
        self.setCentralWidget(tab)
        self._connecting = None
        self.m_sess.setEnabled(True)
        self.act_session_log.setText(
            "セッションログを停止" if (tab.session_log and tab.session_log.is_open())
            else "セッションログを開始")
        self.act_session_log.setEnabled(tab.terminal is not None)
        label = session.profile.label()
        self.setWindowTitle(label)
        # 切断検知はターミナルの session_closed か、SFTP のみなら死活ポーリング
        if tab.terminal is not None:
            tab.terminal.session_closed.connect(self._mark_disconnected)
            tab.terminal.session_closed.connect(self._on_session_closed)
        self._record_last_connected(session.profile)
        self.statusBar().showMessage(f"{label} に接続しました", 5000)
        self._reconnect_attempts = 0
        self._reconnecting = False
        self._reconnect_bar.setVisible(False)
        self._alive_timer.start()

    def _mark_disconnected(self):
        self.setWindowTitle(self.windowTitle().removesuffix(" (切断)") + " (切断)")

    def _on_session_closed(self):
        if self._reconnecting or self.session_tab is None:
            return
        self._alive_timer.stop()
        self._mark_disconnected()
        self._schedule_reconnect()

    def _check_alive(self):
        tab = self.session_tab
        if self._reconnecting or tab is None:
            return
        if tab.session.is_alive():
            return
        self._on_session_closed()

    def _schedule_reconnect(self, manual: bool = False):
        if manual:
            self._reconnect_timer.stop()
            self._reconnect_attempts = 0
            self._start_reconnect()
            return
        if self._reconnecting:
            return
        self._reconnecting = True
        self._reconnect_bar.setVisible(True)
        max_retries = self.settings.get("auto_reconnect_max")
        if self.settings.get("auto_reconnect") and self._reconnect_attempts < max_retries:
            delay = min(60, 5 * (2 ** self._reconnect_attempts))
            self.statusBar().showMessage(
                f"再接続を {delay} 秒後に試行します…", delay * 1000)
            self._reconnect_timer.start(delay * 1000)
        else:
            self.statusBar().showMessage("接続が切断されました。再接続ボタンを押してください")

    def _manual_reconnect(self):
        self._schedule_reconnect(manual=True)

    def _start_reconnect(self):
        self._reconnect_attempts += 1
        self.statusBar().showMessage("再接続中…")
        worker = ConnectWorker(self.profile, self.known_hosts, self.credentials,
                               self.settings)
        worker.ask_secret.connect(
            lambda prompt, ds, cs, w=worker:
            w.provide(SecretDialog.ask(self, prompt, ds, cs))
        )
        worker.ask_hostkey.connect(
            lambda info, w=worker: w.provide(HostKeyDialog.ask(self, info))
        )
        worker.ok.connect(lambda s, w=worker: self._on_reconnect_ok(s, w))
        worker.fail.connect(lambda msg, w=worker: self._on_reconnect_fail(msg, w))
        worker.finished.connect(lambda w=worker: self._workers.remove(w))
        self._workers.append(worker)
        worker.start()

    def _on_reconnect_ok(self, session: SshSession, worker: ConnectWorker):
        tab = self.session_tab
        if tab is None:
            session.close()
            return
        self._reconnecting = False
        self._reconnect_bar.setVisible(False)
        tab.reconnect_session(session)
        self._reconnect_attempts = 0
        label = session.profile.label()
        self.setWindowTitle(label)
        self.statusBar().showMessage(f"{label} に再接続しました", 5000)
        self._alive_timer.start()

    def _on_reconnect_fail(self, msg: str, worker: ConnectWorker):
        max_retries = self.settings.get("auto_reconnect_max")
        if self._reconnect_attempts < max_retries and self.settings.get("auto_reconnect"):
            delay = min(60, 5 * (2 ** self._reconnect_attempts))
            self.statusBar().showMessage(
                f"再接続に失敗しました: {msg}. {delay} 秒後に再試行します…")
            self._reconnect_timer.start(delay * 1000)
        else:
            self.statusBar().showMessage(
                f"再接続できませんでした: {msg}", 5000)

    def _on_connect_failed(self, msg: str):
        if self._connecting is not None:
            self._connecting.show_error(msg)
        state = "接続失敗" if msg else "接続中止"
        self.setWindowTitle(f"{state}: {self.profile.label()}")
        self.statusBar().showMessage(
            "接続に失敗しました" if msg else "接続を中止しました", 4000)

    # ---- セッション操作 -----------------------------------------------------
    def _add_tunnel(self):
        tab = self.session_tab
        if not tab:
            return
        dlg = TunnelDialog(self)
        if dlg.exec():
            try:
                label = tab.add_tunnel(dlg.result())
                self.statusBar().showMessage(f"ポートフォワード開始: {label}", 5000)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "ポートフォワード", f"開始できません:\n{e}")

    def _list_tunnels(self):
        tab = self.session_tab
        if not tab or not tab.tunnels:
            QMessageBox.information(self, "ポートフォワード", "有効なトンネルはありません。")
            return
        lines = "\n".join(f"・{fw.label()}" for fw in tab.tunnels)
        QMessageBox.information(self, "ポートフォワード", lines)

    def _forget_credentials(self):
        tab = self.session_tab
        if tab is None:
            return
        prof = tab.session.profile
        self.credentials.clear_profile(prof)
        self.statusBar().showMessage(
            f"{prof.label()} の保存パスワードを削除しました", 5000)

    def _font_delta(self, d: int):
        tab = self.session_tab
        if tab is not None and tab.terminal is not None:
            tab.terminal.set_font_size(tab.terminal.font_size() + d)

    def _toggle_session_log(self):
        tab = self.session_tab
        if tab is None:
            return
        started = tab.toggle_session_log()
        self.act_session_log.setText(
            "セッションログを停止" if started else "セッションログを開始")
        if started and tab.session_log.path():
            self.statusBar().showMessage(
                f"セッションログ開始: {tab.session_log.path()}", 5000)
        else:
            self.statusBar().showMessage("セッションログを停止しました", 5000)

    # ---- sshd 堅牢化 (Issue #12) --------------------------------------------
    def _harden_sshd(self):
        tab = self.session_tab
        if not tab:
            return
        session = tab.session
        session._hashi_sudo_pw = tab.secret_ctx.get_sudo_password()
        try:
            eff = sshd_admin.read_effective(session)
        except sshd_admin.SshdAdminError as e:
            QMessageBox.warning(self, "SSH サーバー設定", str(e))
            return
        cur_ports = eff["port"] or [22]
        cur_port = cur_ports[0]
        pw_enabled = eff.get("passwordauthentication") != "no"
        dlg = SshdHardenDialog(self, current_port=cur_port,
                               password_enabled=pw_enabled,
                               current_ports=cur_ports)
        if not dlg.exec():
            return
        changes = dlg.result_settings()
        if changes["disable_password"] is None and changes["new_port"] is None:
            return

        summary = []
        if changes["disable_password"]:
            summary.append("・パスワード認証を無効化(鍵認証のみに)")
        if changes["new_port"] is not None:
            summary.append(f"・ポート番号を {cur_port} → {changes['new_port']} へ変更")
        extra = ("<br>成功すると接続プロファイルのポートも自動更新されます。"
                 if changes["new_port"] is not None else "")
        if not DoubleCheckDialog.confirm(
                self, "SSH サーバー設定の変更",
                "以下のサーバー設定を変更します。<br>" + "<br>".join(summary)
                + "<br><br>誤ると SSH に接続できなくなる可能性があります"
                "(変更前にバックアップし、疎通確認に失敗したら自動で戻します)。"
                + extra,
                "change", "変更を適用"):
            return

        sudo_pw = tab.secret_ctx.get_sudo_password()
        worker = SshdHardenWorker(session, session.profile, self.known_hosts,
                                  self.credentials, sudo_pw, changes)
        worker.ok.connect(self._on_sshd_ok)
        worker.fail.connect(
            lambda msg: QMessageBox.warning(self, "SSH サーバー設定", msg))
        worker.finished.connect(lambda w=worker: self._sshd_workers.remove(w))
        self._sshd_workers.append(worker)
        self.statusBar().showMessage("SSH サーバー設定を変更しています…")
        worker.start()

    def _on_sshd_ok(self, res: dict):
        self.statusBar().showMessage("SSH サーバー設定を変更しました", 5000)
        new_port = res.get("new_port")
        note = ""
        if new_port is not None:
            updated = self._update_profile_fields(port=int(new_port))
            note = (f"\n接続プロファイルのポートを {new_port} に自動更新しました。"
                    "次回から新ポートで接続します。" if updated else
                    "\n接続プロファイルは見つからなかったため未更新です。"
                    f"次回接続の前にポートを {new_port} へ変更してください。")
        QMessageBox.information(
            self, "SSH サーバー設定",
            "設定を変更しました。\n"
            f"バックアップ: {res.get('backup')}\n"
            f"適用ファイル: {res.get('dropin')}" + note)

    # ---- 静的 IP 設定 (Issue #45) -------------------------------------------
    def _static_ip(self):
        tab = self.session_tab
        if not tab:
            return
        session = tab.session
        session._hashi_sudo_pw = tab.secret_ctx.get_sudo_password()
        if not netadmin.detect_netplan(session):
            QMessageBox.warning(
                self, "IP 固定",
                "この環境は netplan(Ubuntu Server)で管理されていません。"
                "安全に自動編集できないため中止しました。")
            return
        if netadmin.consume_rollback_marker(session):
            QMessageBox.information(
                self, "IP 固定",
                "前回の IP 固定は確定されず、自動ロールバックで元の設定に"
                "戻っています(インターフェースに旧 IP が残って見える場合は"
                "再起動で消えます)。")
        try:
            interfaces = netadmin.list_interfaces(session)
        except netadmin.NetAdminError as e:
            QMessageBox.warning(self, "IP 固定", str(e))
            return
        gateway = netadmin.current_gateway(session)
        dlg = NetAdminDialog(self, interfaces=interfaces,
                             default_gateway=gateway, default_dns="1.1.1.1")
        if not dlg.exec():
            return
        cfg = dlg.result_settings()
        replace_note = ("<br>⚠ 前回 Hashi が固定した設定(90-hashi.yaml)が"
                        "見つかりました。<b>今回の内容で置き換えます</b>。"
                        if netadmin.dropin_exists(session) else "")
        if not DoubleCheckDialog.confirm(
                self, "サーバーの IP 固定",
                f"インターフェース <b>{cfg['iface']}</b> を "
                f"<b>{cfg['address_cidr']}</b> に固定します。"
                + replace_note + "<br>"
                "誤ると SSH に接続できなくなる可能性があります"
                f"(適用前にバックアップし、{cfg['rollback_sec']} 秒以内に新しい IP へ"
                "疎通できなければ自動で元へ戻します)。<br>"
                "成功すると接続プロファイルの IP を自動更新し、"
                "この接続(旧 IP)は切断されます。",
                "change", "適用"):
            return
        sudo_pw = tab.secret_ctx.get_sudo_password()
        worker = NetAdminWorker(session, session.profile, self.known_hosts,
                                self.credentials, sudo_pw, cfg)
        worker.ok.connect(self._on_static_ip_ok)
        worker.fail.connect(
            lambda msg: QMessageBox.warning(self, "IP 固定", msg))
        worker.finished.connect(
            lambda w=worker: self._netadmin_workers.remove(w))
        self._netadmin_workers.append(worker)
        self.statusBar().showMessage("サーバーの IP を固定しています…")
        worker.start()

    def _on_static_ip_ok(self, res: dict):
        self.statusBar().showMessage("サーバーの IP を固定しました", 5000)
        new_ip = res.get("new_ip", "")
        updated = self._update_profile_host(new_ip) if new_ip else False
        cleaned = res.get("cleaned") or []
        cleaned_note = (f"残留アドレスを掃除: {', '.join(cleaned)}\n" if cleaned
                        else "残留アドレスの掃除は行われませんでした。"
                             "旧 IP が残っている場合は再起動するか、"
                             f"sudo ip addr del <旧IP/プレフィックス> dev "
                             f"{res.get('iface', '<iface>')} で削除してください。\n")
        profile_note = (f"接続プロファイルの IP を {new_ip} に自動更新しました。\n"
                        if updated else
                        "接続プロファイルは見つからなかったため未更新です。\n")
        QMessageBox.information(
            self, "IP 固定",
            "IP を固定しました。\n"
            f"バックアップ: {res.get('backup')}\n"
            f"適用ファイル: {res.get('dropin')}\n"
            + cleaned_note + profile_note +
            "\n旧 IP のこの接続は切断されるため、ウィンドウを閉じます。"
            "サーバー一覧から新しい IP で再接続してください。")
        self._close_sessions_for_profile()

    def _record_last_connected(self, profile: Profile):
        """接続成功日時をプロファイルへ記録し、ランチャーの一覧を更新(#81)。"""
        pid = profile.id_str()
        updated = False
        for p in self.store.profiles:
            if p.id_str() == pid:
                p.last_connected = time.time()
                updated = True
        if updated:
            self.store.save()
        launcher = LauncherWindow._instance
        if launcher is not None:
            try:
                launcher._reload_list()
            except RuntimeError:
                logger.debug("ランチャーは閉じられているため一覧更新をスキップ")

    def _update_profile_host(self, new_ip: str) -> bool:
        """保存済みプロファイルのホストを新 IP に書き換える(#61)。"""
        return self._update_profile_fields(host=new_ip)

    def _update_profile_fields(self, **fields) -> bool:
        """この接続のプロファイルを書き換えて保存する(#61/#62 共通)。"""
        tab = self.session_tab
        if not tab:
            return False
        old_id = tab.session.profile.id_str()
        updated = False
        for p in self.store.profiles:
            if p.id_str() == old_id:
                for key, value in fields.items():
                    setattr(p, key, value)
                updated = True
        if updated:
            self.store.save()
        return updated

    def _close_sessions_for_profile(self):
        """同じプロファイル(旧 IP)で開いている全セッションウィンドウを閉じる。"""
        tab = self.session_tab
        if not tab:
            self.close()
            return
        old_id = tab.session.profile.id_str()
        for win in list(SessionWindow._windows):
            wtab = win.session_tab
            if wtab and wtab.session.profile.id_str() == old_id:
                win.close()
        if self in SessionWindow._windows:
            self.close()

    def closeEvent(self, ev):
        tab = self.session_tab
        if tab is not None:
            if tab.browser is not None and tab.browser.has_active_transfers():
                r = QMessageBox.question(
                    self, "転送中です",
                    "ファイル転送が進行中です。中断して切断しますか?",
                )
                if r != QMessageBox.Yes:
                    ev.ignore()
                    return
            tab.shutdown()
        self._alive_timer.stop()
        self._reconnect_timer.stop()
        if self in SessionWindow._windows:
            SessionWindow._windows.remove(self)
        ev.accept()


# 後方互換: 旧名 MainWindow はランチャーを指す
MainWindow = LauncherWindow
