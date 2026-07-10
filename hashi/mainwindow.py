"""メインウィンドウ。

左: 接続プロファイル一覧 / 右: セッションタブ
各タブ = ターミナル + SFTP ブラウザ (QSplitter で並列表示、片方だけの表示も可)
接続処理はワーカースレッドで行い、パスワード入力やホスト鍵確認だけ
GUI スレッドに問い合わせる。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout, QHBoxLayout, QSplitter, QTabWidget, QMessageBox, QMenu,
    QLabel, QToolButton,
)

from .config import Profile, ProfileStore, KnownHosts, APP_VERSION, Settings
from .ssh_core import SshSession, ConnectCancelled
from .terminal import TerminalWidget
from .filebrowser import SftpBrowser
from .credentials import CredentialStore
from .forward import LocalForward
from .dialogs import (
    ConnectDialog, HostKeyDialog, SecretDialog, SettingsDialog, TunnelDialog,
    ask_secret,
)


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
                 credentials: CredentialStore | None = None):
        super().__init__()
        self.profile = profile
        self.known_hosts = known_hosts
        self.credentials = credentials
        self._evt = threading.Event()
        self._resp = None
        self._tried_kinds: set[str] = set()
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
        kind = "passphrase" if "passphrase" in prompt.lower() else "password"
        # 1) 保存済みを最初の 1 回だけ試す
        if self.credentials and kind not in self._tried_kinds:
            self._tried_kinds.add(kind)
            saved = self.credentials.get(self.profile, kind)
            if saved:
                self._remember(kind, saved)
                return saved
        # 2) 入力を求める
        default_save = self.profile.save_secrets
        can_save = bool(self.credentials and self.credentials.available)
        resp = self._blocking_ask(
            lambda: self.ask_secret.emit(prompt, default_save, can_save))
        secret, save = resp if isinstance(resp, tuple) else (resp, False)
        if secret is None:
            return None
        if save and self.credentials:
            self.credentials.set(self.profile, kind, secret)
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
        try:
            session.connect(self)
            self.ok.emit(session)
        except ConnectCancelled:
            self.fail.emit("")
        except Exception as e:  # noqa: BLE001
            # 認証失敗時、保存済みが誤りだった可能性 → 保存を消しておく
            if self.credentials and "auth" in str(e).lower():
                pass
            self.fail.emit(str(e))


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


class SessionTab(QWidget):
    """1 接続分のタブ。ターミナルと SFTP ブラウザを横並びで持つ。"""

    def __init__(self, session: SshSession, settings: Settings,
                 secret_ctx: SecretContext, parent=None):
        super().__init__(parent)
        self.session = session
        self.settings = settings
        self.secret_ctx = secret_ctx
        self.tunnels: list[LocalForward] = []
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
        info = QLabel(f"{session.profile.username}@{session.profile.host}:{session.profile.port}")
        info.setStyleSheet("color:#8a919e;")
        bar.addWidget(self.bt_term)
        bar.addWidget(self.bt_files)
        bar.addStretch(1)
        bar.addWidget(info)
        root.addLayout(bar)

        self.splitter = QSplitter(Qt.Horizontal)
        self.terminal = TerminalWidget(
            font_size=settings.get("terminal_font_size"),
            right_click_paste=settings.get("right_click_paste"),
        )
        self.browser = SftpBrowser(
            session, session.profile.initial_path,
            settings=settings,
            sudo_provider=lambda: self.secret_ctx.get_sudo_password(),
            sudo_provider_silent=lambda: self.secret_ctx.get_sudo_password(
                allow_prompt=False),
            parent=self,
        )
        self.splitter.addWidget(self.terminal)
        self.splitter.addWidget(self.browser)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        root.addWidget(self.splitter, 1)

        self.bt_term.toggled.connect(self._apply_visibility)
        self.bt_files.toggled.connect(self._apply_visibility)
        self.terminal.password_prompt.connect(self._on_password_prompt)

        # トースト(自動入力などの通知)
        self._toast = QLabel(self)
        self._toast.setStyleSheet(
            "background:#2d333f; color:#dcdfe4; border:1px solid #444c56;"
            "border-radius:6px; padding:6px 12px;")
        self._toast.setVisible(False)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self._toast.setVisible(False))

        # シェル開始
        ch = session.open_shell()
        self.terminal.attach(ch)
        self.terminal.setFocus()

    # ---- パスワード自動入力 -------------------------------------------------
    def _on_password_prompt(self, kind: str):
        import time
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
                self._flash("sudo パスワード要求: 右クリック→送信 で入力できます")
                return
            now = time.monotonic()
            if now - self._last_autofill_ts < 8.0:
                # 直前に自動入力した直後の再要求 = おそらく誤り。手動に委ねる
                self._flash("パスワードが違うようです。手動で入力してください", warn=True)
                return
            pw = self.secret_ctx.get_sudo_password()
            if pw:
                self._last_autofill_ts = now
                self.terminal.send_password(pw)
                self._flash("🔑 sudo パスワードを自動入力しました")
        elif kind in ("password", "passphrase"):
            # 別ホストの可能性があるので自動送信しない(手動送信は可能)
            self._flash("パスワード要求: 右クリック→送信 で保存済みを送れます")

    def _flash(self, text: str, warn: bool = False):
        self._toast.setText(text)
        self._toast.setStyleSheet(
            "background:%s; color:#fff; border-radius:6px; padding:6px 12px;"
            % ("#7a3b3b" if warn else "#2d333f"))
        self._toast.adjustSize()
        self._toast.move(max(12, (self.width() - self._toast.width()) // 2), 40)
        self._toast.setVisible(True)
        self._toast.raise_()
        self._toast_timer.start(3500)

    # ---- ポートフォワード ----------------------------------------------------
    def add_tunnel(self, spec: dict) -> str:
        fw = LocalForward(
            self.session.transport, spec["local_host"], spec["local_port"],
            spec["remote_host"], spec["remote_port"])
        fw.start()
        self.tunnels.append(fw)
        return fw.label()

    def _apply_visibility(self):
        if not self.bt_term.isChecked() and not self.bt_files.isChecked():
            sender = self.sender()
            other = self.bt_files if sender is self.bt_term else self.bt_term
            other.setChecked(True)
        self.terminal.setVisible(self.bt_term.isChecked())
        self.browser.setVisible(self.bt_files.isChecked())

    def shutdown(self):
        for fw in self.tunnels:
            fw.stop()
        self.browser.shutdown()
        self.terminal.detach()
        self.session.close()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Hashi — SSH / SFTP クライアント v{APP_VERSION}")
        self.resize(1280, 760)

        self.store = ProfileStore()
        self.known_hosts = KnownHosts()
        self.settings = Settings()
        self.credentials = CredentialStore()
        self._workers: list[ConnectWorker] = []

        # サイドバー
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(6, 6, 6, 6)
        btn_new = QPushButton("＋ 新しい接続")
        btn_new.clicked.connect(self.new_profile)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._connect_item)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._profile_menu)
        hint = QLabel("ダブルクリックで接続\n右クリックで編集/削除")
        hint.setStyleSheet("color:#8a919e;")
        sv.addWidget(btn_new)
        sv.addWidget(self.list, 1)
        sv.addWidget(hint)
        side.setMaximumWidth(240)

        # タブ
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        placeholder = QLabel(
            "左のプロファイルをダブルクリックして接続\n"
            "または「＋ 新しい接続」から追加"
        )
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setStyleSheet("color:#8a919e; font-size:14px;")
        self.tabs.addTab(placeholder, "ようこそ")

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(side)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        self._build_menu()
        self._reload_list()
        self.statusBar().showMessage("準備完了")

    # ---- メニュー -----------------------------------------------------------
    def _build_menu(self):
        m_file = self.menuBar().addMenu("ファイル")
        m_file.addAction("新しい接続…", self.new_profile)
        m_file.addSeparator()
        m_file.addAction("設定…", self._open_settings)
        m_file.addSeparator()
        m_file.addAction("終了", self.close)

        m_view = self.menuBar().addMenu("表示")
        act_plus = m_view.addAction("ターミナル文字を大きく")
        act_plus.setShortcut("Ctrl+=")
        act_plus.triggered.connect(lambda: self._font_delta(+1))
        act_minus = m_view.addAction("ターミナル文字を小さく")
        act_minus.setShortcut("Ctrl+-")
        act_minus.triggered.connect(lambda: self._font_delta(-1))

        m_sess = self.menuBar().addMenu("セッション")
        m_sess.addAction("ポートフォワードを追加…", self._add_tunnel)
        m_sess.addAction("ポートフォワード一覧…", self._list_tunnels)
        m_sess.addSeparator()
        m_sess.addAction("この接続の保存パスワードを削除", self._forget_credentials)

        m_help = self.menuBar().addMenu("ヘルプ")
        m_help.addAction("Hashi について", self._about)

    def _open_settings(self):
        SettingsDialog(self.settings, self).exec()

    def _current_tab(self):
        w = self.tabs.currentWidget()
        return w if isinstance(w, SessionTab) else None

    def _add_tunnel(self):
        tab = self._current_tab()
        if not tab:
            QMessageBox.information(self, "ポートフォワード", "接続中のタブがありません。")
            return
        dlg = TunnelDialog(self)
        if dlg.exec():
            try:
                label = tab.add_tunnel(dlg.result())
                self.statusBar().showMessage(f"ポートフォワード開始: {label}", 5000)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "ポートフォワード", f"開始できません:\n{e}")

    def _list_tunnels(self):
        tab = self._current_tab()
        if not tab or not tab.tunnels:
            QMessageBox.information(self, "ポートフォワード", "有効なトンネルはありません。")
            return
        lines = "\n".join(f"・{fw.label()}" for fw in tab.tunnels)
        QMessageBox.information(self, "ポートフォワード", lines)

    def _forget_credentials(self):
        tab = self._current_tab()
        prof = tab.session.profile if tab else None
        if prof is None:
            QMessageBox.information(self, "認証情報", "接続中のタブがありません。")
            return
        self.credentials.clear_profile(prof)
        self.statusBar().showMessage(
            f"{prof.label()} の保存パスワードを削除しました", 5000)

    def _font_delta(self, d: int):
        tab = self.tabs.currentWidget()
        if isinstance(tab, SessionTab):
            tab.terminal.set_font_size(tab.terminal.font_size() + d)

    def _about(self):
        QMessageBox.information(
            self, "Hashi について",
            f"Hashi v{APP_VERSION}\n\n"
            "SSH ターミナル + SFTP ファイルブラウザ\n"
            "橋 (bridge) — ローカルとリモートをつなぐ。\n\n"
            "Python / PySide6 / paramiko / pyte",
        )

    # ---- プロファイル管理 --------------------------------------------------------
    def _reload_list(self):
        self.list.clear()
        for p in self.store.profiles:
            item = QListWidgetItem(p.label())
            item.setToolTip(f"{p.username}@{p.host}:{p.port} ({p.auth_method})")
            self.list.addItem(item)

    def new_profile(self):
        dlg = ConnectDialog(self)
        if dlg.exec():
            self.store.add(dlg.result_profile())
            self._reload_list()

    def _profile_menu(self, pos):
        row = self.list.currentRow()
        if row < 0:
            return
        menu = QMenu(self)
        a_conn = menu.addAction("接続")
        a_edit = menu.addAction("編集…")
        a_del = menu.addAction("削除")
        chosen = menu.exec(self.list.viewport().mapToGlobal(pos))
        if chosen is a_conn:
            self._connect_profile(self.store.profiles[row])
        elif chosen is a_edit:
            dlg = ConnectDialog(self, self.store.profiles[row])
            if dlg.exec():
                self.store.update(row, dlg.result_profile())
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
        row = self.list.row(item)
        if 0 <= row < len(self.store.profiles):
            self._connect_profile(self.store.profiles[row])

    # ---- 接続 --------------------------------------------------------------
    def _connect_profile(self, profile: Profile):
        self.statusBar().showMessage(f"{profile.label()} に接続中…")
        worker = ConnectWorker(profile, self.known_hosts, self.credentials)
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
        # 初回接続でようこそタブを消す
        if self.tabs.count() == 1 and not isinstance(self.tabs.widget(0), SessionTab):
            self.tabs.removeTab(0)
        ctx = SecretContext(session.profile, self.credentials,
                            self.settings, self)
        if worker.used_password:
            ctx.note_login_password(worker.used_password)
        tab = SessionTab(session, self.settings, ctx)
        label = session.profile.label()
        idx = self.tabs.addTab(tab, label)
        self.tabs.setCurrentIndex(idx)
        tab.terminal.session_closed.connect(
            lambda i=idx: self._mark_disconnected(tab)
        )
        self.statusBar().showMessage(f"{label} に接続しました", 5000)

    def _mark_disconnected(self, tab: "SessionTab"):
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.setTabText(idx, self.tabs.tabText(idx) + " (切断)")

    def _on_connect_failed(self, msg: str):
        self.statusBar().showMessage("接続を中止しました", 4000)
        if msg:
            QMessageBox.warning(self, "接続エラー", msg)

    # ---- タブ/終了 -----------------------------------------------------------
    def _close_tab(self, index: int):
        w = self.tabs.widget(index)
        if isinstance(w, SessionTab):
            if w.browser.has_active_transfers():
                r = QMessageBox.question(
                    self, "転送中です",
                    "ファイル転送が進行中です。中断して切断しますか?",
                )
                if r != QMessageBox.Yes:
                    return
            w.shutdown()
        self.tabs.removeTab(index)

    def closeEvent(self, ev):
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SessionTab):
                w.shutdown()
        ev.accept()
