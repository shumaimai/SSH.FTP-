"""ダイアログ群。

- DoubleCheckDialog: 破壊的操作(削除/上書き)の 2 段階目。確認語の入力を要求
- HostKeyDialog: ホスト鍵フィンガープリントの確認 (初回 / 変更検出)
- ConnectDialog: 接続プロファイルの新規作成・編集
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import style
from .config import AUTH_AGENT, AUTH_KEY, AUTH_PASSWORD, Profile
from .keygen import ECDSA_BITS, RSA_BITS


class DoubleCheckDialog(QDialog):
    """確認語を正しく入力するまで実行ボタンが押せないダイアログ。"""

    def __init__(self, parent, title: str, message_html: str,
                 confirm_word: str, action_label: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)

        lay = QVBoxLayout(self)
        msg = QLabel(message_html)
        msg.setTextFormat(Qt.RichText)
        msg.setWordWrap(True)
        lay.addWidget(msg)

        hint = QLabel(
            f"実行するには下の欄に <b>{confirm_word}</b> と入力してください。"
        )
        hint.setTextFormat(Qt.RichText)
        lay.addWidget(hint)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText(confirm_word)
        lay.addWidget(self._edit)

        self._buttons = QDialogButtonBox()
        self._ok = self._buttons.addButton(action_label, QDialogButtonBox.AcceptRole)
        self._buttons.addButton("キャンセル", QDialogButtonBox.RejectRole)
        self._ok.setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        lay.addWidget(self._buttons)

        word = confirm_word
        self._edit.textChanged.connect(
            lambda t: self._ok.setEnabled(t.strip() == word)
        )
        self._edit.setFocus()

    @staticmethod
    def confirm(parent, title: str, message_html: str,
                confirm_word: str, action_label: str) -> bool:
        dlg = DoubleCheckDialog(parent, title, message_html, confirm_word, action_label)
        return dlg.exec() == QDialog.Accepted


class HostKeyDialog(QDialog):
    """ホスト鍵の受け入れ確認。鍵変更(mismatch)時は強い警告を出す。"""

    def __init__(self, parent, info: dict):
        super().__init__(parent)
        self.setModal(True)
        self.setMinimumWidth(480)
        mismatch = info.get("status") == "mismatch"
        self.setWindowTitle("ホスト鍵の変更を検出" if mismatch else "初めて接続するホストです")

        lay = QVBoxLayout(self)
        if mismatch:
            warn = QLabel(
                f"<b style='color:{style.ERROR}; font-size:14px;'>"
                "⚠ 警告: このホストの鍵が以前と異なります。</b><br>"
                "サーバーの再構築が原因の場合もありますが、"
                "<b>中間者攻撃(なりすまし)の可能性</b>もあります。<br>"
                "心当たりがない場合は接続を中止してください。"
            )
        else:
            warn = QLabel(
                f"<b>{info['host']}:{info['port']}</b> には初めて接続します。<br>"
                "以下のフィンガープリントがサーバー管理者の提示するものと"
                "一致するか確認してください。"
            )
        warn.setTextFormat(Qt.RichText)
        warn.setWordWrap(True)
        lay.addWidget(warn)

        mono = QFont()
        mono.setFamilies(["Consolas", "Monospace"])
        fp = QLabel(f"{info['key_type']}\n{info['fingerprint']}")
        fp.setFont(mono)
        fp.setTextInteractionFlags(Qt.TextSelectableByMouse)
        fp.setStyleSheet("padding:8px; background:#22262e; border-radius:4px;")
        lay.addWidget(fp)

        if mismatch and info.get("old_fingerprint"):
            old = QLabel(f"記録済みの鍵: {info['old_fingerprint']}")
            old.setFont(mono)
            old.setStyleSheet(f"color:{style.FG_MUTED};")
            lay.addWidget(old)

        buttons = QDialogButtonBox()
        ok = buttons.addButton("信頼して接続", QDialogButtonBox.AcceptRole)
        cancel = buttons.addButton("中止", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        (cancel if mismatch else ok).setDefault(True)
        self._mismatch = mismatch

    @staticmethod
    def ask(parent, info: dict) -> bool:
        dlg = HostKeyDialog(parent, info)
        if dlg.exec() != QDialog.Accepted:
            return False
        # 鍵変更時はさらに 2 段階目 (typed confirm)
        if dlg._mismatch:
            return DoubleCheckDialog.confirm(
                parent,
                "本当に信頼しますか?",
                "変更されたホスト鍵を信頼して記録を上書きします。",
                "trust",
                "上書きして接続",
            )
        return True


class SecretDialog(QDialog):
    """パスワード/パスフレーズ入力 + 「次回から保存する」チェック。"""

    def __init__(self, parent, prompt: str, default_save: bool = True,
                 can_save: bool = True):
        super().__init__(parent)
        self.setWindowTitle("認証")
        self.setModal(True)
        self.setMinimumWidth(400)
        lay = QVBoxLayout(self)
        lb = QLabel(prompt)
        lb.setWordWrap(True)
        lay.addWidget(lb)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.returnPressed.connect(self.accept)
        lay.addWidget(self.edit)
        self.chk_save = QCheckBox("次回から保存する")
        self.chk_save.setChecked(default_save)
        self.chk_save.setEnabled(can_save)
        if not can_save:
            self.chk_save.setToolTip("認証情報ストアが利用できません")
        lay.addWidget(self.chk_save)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.edit.setFocus()

    @staticmethod
    def ask(parent, prompt: str, default_save: bool = True, can_save: bool = True):
        dlg = SecretDialog(parent, prompt, default_save, can_save)
        if dlg.exec() == QDialog.Accepted:
            return dlg.edit.text(), dlg.chk_save.isChecked()
        return None, False


def ask_secret(parent, prompt: str) -> str | None:
    """後方互換用のシンプルなパスワード入力(保存なし)。"""
    text, ok = QInputDialog.getText(parent, "認証", prompt, QLineEdit.Password)
    return text if ok else None


class ConnectDialog(QDialog):
    """接続プロファイルの作成・編集。"""

    def __init__(self, parent=None, profile: Profile | None = None,
                 credentials=None):
        super().__init__(parent)
        self.setWindowTitle("接続設定")
        self.setModal(True)
        self.setMinimumWidth(460)
        p = profile or Profile()
        self._previous_profile = profile
        self._credentials = credentials

        form = QFormLayout()
        self.ed_name = QLineEdit(p.name)
        self.ed_name.setPlaceholderText("例: 自宅サーバー (省略可)")
        self.ed_host = QLineEdit(p.host)
        self.ed_host.setPlaceholderText("例: 192.168.1.10 / example.com")
        self.sp_port = QSpinBox()
        self.sp_port.setRange(1, 65535)
        self.sp_port.setValue(p.port)
        self.ed_user = QLineEdit(p.username)

        self.cb_auth = QComboBox()
        self.cb_auth.addItem("公開鍵 (秘密鍵ファイル)", AUTH_KEY)
        self.cb_auth.addItem("パスワード", AUTH_PASSWORD)
        self.cb_auth.addItem("SSH エージェント", AUTH_AGENT)
        idx = self.cb_auth.findData(p.auth_method)
        self.cb_auth.setCurrentIndex(max(0, idx))

        key_row = QWidget()
        key_lay = QHBoxLayout(key_row)
        key_lay.setContentsMargins(0, 0, 0, 0)
        self.ed_key = QLineEdit(p.key_path)
        self.ed_key.setPlaceholderText("例: C:\\Users\\you\\.ssh\\id_ed25519")
        btn_browse = QPushButton("参照…")
        btn_browse.clicked.connect(self._browse_key)
        key_lay.addWidget(self.ed_key)
        key_lay.addWidget(btn_browse)
        self._key_row = key_row

        self.ed_password = QLineEdit()
        self.ed_password.setEchoMode(QLineEdit.Password)
        self.ed_password.setPlaceholderText(
            "空欄なら保存済みの内容を変更しません"
            if profile else "空欄なら接続時に入力"
        )

        self.ed_passphrase = QLineEdit()
        self.ed_passphrase.setEchoMode(QLineEdit.Password)
        self.ed_passphrase.setPlaceholderText(
            "空欄なら保存済みの内容を変更しません"
            if profile else "空欄なら必要時に入力"
        )

        self.ed_initial = QLineEdit(p.initial_path)
        self.ed_initial.setPlaceholderText("空欄ならホームディレクトリ")

        self.ed_proxy = QLineEdit(p.proxy_jump)
        self.ed_proxy.setPlaceholderText(
            "例: user@bastion:22 (カンマ区切りで多段。空欄なら直接接続)")

        self.chk_save = QCheckBox("入力したパスワード/パスフレーズを保存する")
        self.chk_save.setChecked(p.save_secrets)
        self.chk_sudo = QCheckBox("sudo のパスワードはログインと同じ")
        self.chk_sudo.setChecked(p.sudo_same_as_password)

        form.addRow("名前", self.ed_name)
        form.addRow("ホスト", self.ed_host)
        form.addRow("ポート", self.sp_port)
        form.addRow("ユーザー名", self.ed_user)
        form.addRow("認証方式", self.cb_auth)
        form.addRow("秘密鍵", key_row)
        form.addRow("パスワード", self.ed_password)
        form.addRow("鍵のパスフレーズ", self.ed_passphrase)
        form.addRow("初期パス", self.ed_initial)
        form.addRow("踏み台 (ProxyJump)", self.ed_proxy)
        form.addRow("", self.chk_save)
        form.addRow("", self.chk_sudo)
        self._key_label = form.labelForField(self._key_row)
        self._password_label = form.labelForField(self.ed_password)
        self._passphrase_label = form.labelForField(self.ed_passphrase)

        note = QLabel(
            "保存する場合、OS の資格情報ストア(Windows 資格情報マネージャ等)を"
            "優先して使います。利用できない環境では暗号化ファイルに保存します。"
        )
        note.setStyleSheet(f"color:{style.FG_MUTED};")
        note.setWordWrap(True)

        buttons = QDialogButtonBox()
        buttons.addButton("保存", QDialogButtonBox.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(note)
        root.addWidget(buttons)

        self.cb_auth.currentIndexChanged.connect(self._toggle_auth_rows)
        self._toggle_auth_rows()

    @staticmethod
    def _set_row_visible(label, widget, visible: bool):
        label.setVisible(visible)
        widget.setVisible(visible)

    def _toggle_auth_rows(self):
        auth_method = self.cb_auth.currentData()
        is_key = auth_method == AUTH_KEY
        has_password = auth_method in (AUTH_KEY, AUTH_PASSWORD)
        self._set_row_visible(self._key_label, self._key_row, is_key)
        self._set_row_visible(
            self._password_label, self.ed_password, has_password)
        self._set_row_visible(
            self._passphrase_label, self.ed_passphrase, is_key)
        self._password_label.setText(
            "ログインパスワード（任意）" if is_key else "パスワード")

    def _browse_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "秘密鍵ファイルを選択")
        if path:
            self.ed_key.setText(path)

    def _validate_accept(self):
        if not self.ed_host.text().strip():
            self.ed_host.setFocus()
            return
        if not self.ed_user.text().strip():
            self.ed_user.setFocus()
            return
        if self.cb_auth.currentData() == AUTH_KEY and not self.ed_key.text().strip():
            self.ed_key.setFocus()
            return
        self.accept()

    def result_profile(self) -> Profile:
        return Profile(
            name=self.ed_name.text().strip(),
            host=self.ed_host.text().strip(),
            port=self.sp_port.value(),
            username=self.ed_user.text().strip(),
            auth_method=self.cb_auth.currentData(),
            key_path=self.ed_key.text().strip(),
            initial_path=self.ed_initial.text().strip(),
            proxy_jump=self.ed_proxy.text().strip(),
            save_secrets=self.chk_save.isChecked(),
            sudo_same_as_password=self.chk_sudo.isChecked(),
        )

    def apply_credentials(self, profile: Profile) -> None:
        if self._credentials is None:
            return
        previous = self._previous_profile
        if not profile.save_secrets:
            self._credentials.clear_profile(profile)
            if previous and previous.id_str() != profile.id_str():
                self._credentials.clear_profile(previous)
            return

        auth_method = self.cb_auth.currentData()
        password = self.ed_password.text() if auth_method != AUTH_AGENT else ""
        passphrase = self.ed_passphrase.text() if auth_method == AUTH_KEY else ""
        if password:
            self._credentials.set(profile, "password", password)
        if passphrase:
            self._credentials.set(profile, "passphrase", passphrase)
        if previous and previous.id_str() != profile.id_str():
            self._credentials.clear_profile(previous)


class TunnelDialog(QDialog):
    """ポートフォワード (-L / -R / -D) の追加。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ポートフォワードの追加")
        self.setModal(True)
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self.cb_type = QComboBox()
        self.cb_type.addItem("ローカル (-L)", "local")
        self.cb_type.addItem("リモート (-R)", "remote")
        self.cb_type.addItem("ダイナミック (-D / SOCKS5)", "dynamic")

        self.ed_bind_host = QLineEdit("127.0.0.1")
        self.sp_bind_port = QSpinBox()
        self.sp_bind_port.setRange(0, 65535)
        self.sp_bind_port.setValue(8080)
        self.ed_dest_host = QLineEdit("127.0.0.1")
        self.sp_dest_port = QSpinBox()
        self.sp_dest_port.setRange(1, 65535)
        self.sp_dest_port.setValue(80)

        form.addRow("種別", self.cb_type)
        form.addRow("待受ホスト", self.ed_bind_host)
        form.addRow("待受ポート", self.sp_bind_port)
        form.addRow("転送先ホスト", self.ed_dest_host)
        form.addRow("転送先ポート", self.sp_dest_port)

        self._label_bind_host = form.labelForWidget(self.ed_bind_host)
        self._label_bind_port = form.labelForWidget(self.sp_bind_port)
        self._label_dest_host = form.labelForWidget(self.ed_dest_host)
        self._label_dest_port = form.labelForWidget(self.sp_dest_port)

        self.note = QLabel(
            "例: ローカル 8080 → サーバー側から見た 127.0.0.1:80 に転送。\n"
            "localhost:ローカルポート にアクセスすると、SSH 経由で転送先へ繋がります。"
        )
        self.note.setStyleSheet(f"color:{style.FG_MUTED};")
        self.note.setWordWrap(True)

        buttons = QDialogButtonBox()
        buttons.addButton("追加", QDialogButtonBox.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root.addLayout(form)
        root.addWidget(self.note)
        root.addWidget(buttons)

        self.cb_type.currentIndexChanged.connect(self._on_type_changed)
        self._on_type_changed()

    def _on_type_changed(self):
        kind = self.cb_type.currentData()
        if kind == "local":
            self._label_bind_host.setText("ローカル待受ホスト")
            self._label_bind_port.setText("ローカル待受ポート")
            self._label_dest_host.setText("転送先ホスト(サーバー側から見て)")
            self._label_dest_port.setText("転送先ポート")
            self._set_dest_visible(True)
            self.note.setText(
                "例: ローカル 8080 → サーバー側から見た 127.0.0.1:80 に転送。\n"
                "localhost:ローカルポート にアクセスすると、SSH 経由で転送先へ繋がります。"
            )
        elif kind == "remote":
            self._label_bind_host.setText("リモート待受ホスト(サーバー側)")
            self._label_bind_port.setText("リモート待受ポート")
            self._label_dest_host.setText("転送先ホスト(ローカル)")
            self._label_dest_port.setText("転送先ポート")
            self._set_dest_visible(True)
            self.note.setText(
                "例: サーバー側 8080 → ローカル 127.0.0.1:80 に転送。\n"
                "リモートホスト:ポート にアクセスすると、SSH 経由でローカル転送先へ繋がります。"
            )
        elif kind == "dynamic":
            self._label_bind_host.setText("SOCKS5 待受ホスト")
            self._label_bind_port.setText("SOCKS5 待受ポート")
            self._set_dest_visible(False)
            self.note.setText(
                "例: ブラウザやアプリの SOCKS5 プロキシとして 127.0.0.1:8080 を指定。\n"
                "接続先は動的に決まり、SSH 経由で直接転送されます。"
            )

    def _set_dest_visible(self, visible: bool):
        self._label_dest_host.setVisible(visible)
        self.ed_dest_host.setVisible(visible)
        self._label_dest_port.setVisible(visible)
        self.sp_dest_port.setVisible(visible)

    def result(self):
        kind = self.cb_type.currentData()
        if kind == "remote":
            return {
                "type": "remote",
                "remote_host": self.ed_bind_host.text().strip() or "127.0.0.1",
                "remote_port": self.sp_bind_port.value(),
                "local_host": self.ed_dest_host.text().strip() or "127.0.0.1",
                "local_port": self.sp_dest_port.value(),
            }
        return {
            "type": kind,
            "local_host": self.ed_bind_host.text().strip() or "127.0.0.1",
            "local_port": self.sp_bind_port.value(),
            "remote_host": self.ed_dest_host.text().strip() or "127.0.0.1",
            "remote_port": self.sp_dest_port.value() if kind != "dynamic" else 0,
        }


class KeygenDialog(QDialog):
    """SSH 鍵ペアの生成設定。"""

    def __init__(self, parent=None, can_register: bool = False):
        super().__init__(parent)
        self.setWindowTitle("SSH 鍵を生成")
        self.setModal(True)
        self.setMinimumWidth(500)

        form = QFormLayout()
        self.cb_type = QComboBox()
        self.cb_type.addItem("Ed25519", "ed25519")
        self.cb_type.addItem("ECDSA", "ecdsa")
        self.cb_type.addItem("RSA", "rsa")
        self.cb_bits = QComboBox()
        self.ed_passphrase = QLineEdit()
        self.ed_passphrase.setEchoMode(QLineEdit.Password)
        self.ed_comment = QLineEdit()

        path_row = QWidget()
        path_lay = QHBoxLayout(path_row)
        path_lay.setContentsMargins(0, 0, 0, 0)
        self.ed_path = QLineEdit()
        self.ed_path.setPlaceholderText("保存先の秘密鍵ファイル")
        browse = QPushButton("参照…")
        browse.clicked.connect(self._browse)
        path_lay.addWidget(self.ed_path)
        path_lay.addWidget(browse)

        self.chk_register = QCheckBox("現在の接続先に公開鍵を登録する")
        self.chk_register.setEnabled(can_register)
        if not can_register:
            self.chk_register.setToolTip("接続中のセッションがありません")

        form.addRow("鍵種別", self.cb_type)
        form.addRow("ビット数", self.cb_bits)
        form.addRow("パスフレーズ（任意）", self.ed_passphrase)
        form.addRow("コメント（任意）", self.ed_comment)
        form.addRow("秘密鍵の保存先", path_row)
        form.addRow("", self.chk_register)

        note = QLabel(
            "秘密鍵は指定した場所へ保存し、POSIX 環境では権限を 600 に設定します。"
        )
        note.setStyleSheet(f"color:{style.FG_MUTED};")
        note.setWordWrap(True)

        buttons = QDialogButtonBox()
        buttons.addButton("生成", QDialogButtonBox.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(note)
        root.addWidget(buttons)

        self.cb_type.currentIndexChanged.connect(self._update_bits)
        self._update_bits()

    def _update_bits(self):
        kind = self.cb_type.currentData()
        values = [None] if kind == "ed25519" else (
            list(ECDSA_BITS) if kind == "ecdsa" else list(RSA_BITS)
        )
        self.cb_bits.clear()
        for value in values:
            if value is None:
                self.cb_bits.addItem("（固定）", None)
            else:
                self.cb_bits.addItem(str(value), value)
        self.cb_bits.setEnabled(kind != "ed25519")

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "秘密鍵の保存先を選択")
        if path:
            self.ed_path.setText(path)

    def _validate_accept(self):
        path = self.ed_path.text().strip()
        if not path:
            self.ed_path.setFocus()
            return
        if Path(path).expanduser().exists():
            answer = QMessageBox.question(
                self,
                "秘密鍵の上書き",
                f"既存の秘密鍵ファイルを上書きしますか?\n{path}",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.accept()

    def result_settings(self) -> dict:
        return {
            "key_type": self.cb_type.currentData(),
            "bits": self.cb_bits.currentData(),
            "passphrase": self.ed_passphrase.text() or None,
            "comment": self.ed_comment.text().strip(),
            "path": self.ed_path.text().strip(),
            "register": self.chk_register.isChecked(),
        }


class NetAdminDialog(QDialog):
    """サーバーの静的 IP 設定(Issue #45、netplan 限定)。"""

    def __init__(self, parent=None, interfaces=None,
                 default_rollback: int = 20,
                 default_gateway: str = "", default_dns: str = "1.1.1.1"):
        super().__init__(parent)
        self.setWindowTitle("サーバーの IP を固定 (netplan)")
        self.setModal(True)
        self.setMinimumWidth(520)

        form = QFormLayout()
        self.cb_iface = QComboBox()
        for it in (interfaces or []):
            label = f"{it['name']}  (現在: {it.get('address', '?')})"
            self.cb_iface.addItem(label, it["name"])
        self.cb_iface.setEditable(True)   # 一覧に無い名前も入れられる

        self.ed_address = QLineEdit()
        self.ed_address.setPlaceholderText("例: 192.168.1.50/24 (CIDR 表記)")
        self.ed_gateway = QLineEdit(default_gateway)
        self.ed_gateway.setPlaceholderText("例: 192.168.1.1 (任意)")
        self.ed_dns = QLineEdit(default_dns)
        self.ed_dns.setPlaceholderText("例: 1.1.1.1, 8.8.8.8 (カンマ区切り・任意)")

        self.sp_rollback = QSpinBox()
        self.sp_rollback.setRange(10, 600)
        self.sp_rollback.setValue(default_rollback)
        self.sp_rollback.setSuffix(" 秒")

        form.addRow("インターフェース", self.cb_iface)
        form.addRow("IP アドレス/プレフィックス", self.ed_address)
        form.addRow("ゲートウェイ", self.ed_gateway)
        form.addRow("DNS", self.ed_dns)
        form.addRow("自動ロールバック", self.sp_rollback)

        warn = QLabel(
            "⚠ ネットワーク設定を変更します。誤ると SSH ごと切断されます。安全のため、"
            "適用前にバックアップし、指定秒数内に新しい IP への疎通が確認できなければ"
            "自動で元へ戻します。netplan(Ubuntu Server)以外の環境では実行しません。"
            "\nsudo パスワードが必要です。")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{style.WARN};")

        buttons = QDialogButtonBox()
        buttons.addButton("適用", QDialogButtonBox.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(warn)
        root.addWidget(buttons)

    def _validate_accept(self):
        if not self._iface() or not self.ed_address.text().strip():
            (self.cb_iface if not self._iface() else self.ed_address).setFocus()
            return
        self.accept()

    def _iface(self) -> str:
        data = self.cb_iface.currentData()
        return (data or self.cb_iface.currentText().split()[0]
                if self.cb_iface.currentText() else "").strip()

    def result_settings(self) -> dict:
        dns = [s.strip() for s in self.ed_dns.text().split(",") if s.strip()]
        return {
            "iface": self._iface(),
            "address_cidr": self.ed_address.text().strip(),
            "gateway": self.ed_gateway.text().strip(),
            "nameservers": dns,
            "rollback_sec": self.sp_rollback.value(),
        }


class P2PSendDialog(QDialog):
    """P2P 送信の宛先入力(Issue #43)。"""

    def __init__(self, parent=None, default_port: int = 53517):
        super().__init__(parent)
        self.setWindowTitle("接続情報を送信 (P2P)")
        self.setModal(True)
        self.setMinimumWidth(420)

        form = QFormLayout()
        self.ed_host = QLineEdit()
        self.ed_host.setPlaceholderText("相手の IP アドレス / ホスト名")
        self.sp_port = QSpinBox()
        self.sp_port.setRange(1, 65535)
        self.sp_port.setValue(default_port)
        self.chk_secrets = QCheckBox(
            "保存済みの秘密情報も送る(パスフレーズで暗号化)")
        form.addRow("送信先", self.ed_host)
        form.addRow("ポート", self.sp_port)
        form.addRow("", self.chk_secrets)

        note = QLabel(
            "相手側で「接続情報を受信」を先に開始してください。\n"
            "接続後に表示される確認コードを、電話など別の手段で照合します。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{style.FG_MUTED};")

        buttons = QDialogButtonBox()
        buttons.addButton("接続", QDialogButtonBox.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(note)
        root.addWidget(buttons)

    def _validate_accept(self):
        if self.ed_host.text().strip():
            self.accept()
        else:
            self.ed_host.setFocus()

    def result_target(self) -> dict:
        return {
            "host": self.ed_host.text().strip(),
            "port": self.sp_port.value(),
            "include_secrets": self.chk_secrets.isChecked(),
        }


class SasConfirmDialog(QDialog):
    """確認コード(SAS)の照合ダイアログ(Issue #43)。"""

    def __init__(self, parent, sas: str, role: str):
        super().__init__(parent)
        self.setWindowTitle("確認コードの照合")
        self.setModal(True)
        self.setMinimumWidth(400)
        lay = QVBoxLayout(self)
        msg = QLabel(
            f"{role}の確認コードです。相手と同じか、電話など別の手段で"
            "照合してください。一致していなければ<b>中止</b>してください"
            "(中間者攻撃の可能性)。")
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.RichText)
        lay.addWidget(msg)

        code = QLabel(sas)
        f = QFont()
        f.setPointSize(28)
        f.setBold(True)
        code.setFont(f)
        code.setAlignment(Qt.AlignCenter)
        code.setStyleSheet("letter-spacing:8px; padding:12px;")
        lay.addWidget(code)

        buttons = QDialogButtonBox()
        buttons.addButton("一致している(続行)", QDialogButtonBox.AcceptRole)
        cancel = buttons.addButton("中止", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        cancel.setDefault(True)
        lay.addWidget(buttons)

    @staticmethod
    def confirm(parent, sas: str, role: str) -> bool:
        return SasConfirmDialog(parent, sas, role).exec() == QDialog.Accepted


class SshdHardenDialog(QDialog):
    """sshd 堅牢化(パスワードログイン無効化 / ポート変更、Issue #12)。"""

    def __init__(self, parent=None, current_port: int = 22,
                 password_enabled: bool = True, current_ports=None):
        super().__init__(parent)
        self.setWindowTitle("SSH サーバーの設定を変更")
        self.setModal(True)
        self.setMinimumWidth(500)

        form = QFormLayout()

        # 現在の実効状態を明示する(Issue #73)。sshd -T は Include /
        # sshd_config.d / 設定の優先順を解決した実効値なので、上位設定の
        # 見落としが起きない。ただし Match ブロック適用前のグローバル値。
        ports_str = ", ".join(str(p) for p in (current_ports or [current_port]))
        state = "有効" if password_enabled else "無効"
        self.lbl_state = QLabel(
            f"現在の状態: パスワード認証は <b>{state}</b> / "
            f"待受ポート: <b>{ports_str}</b>"
            f"<br><span style='color:{style.FG_MUTED};'>(sshd -T の実効値。Include や "
            "sshd_config.d を解決済み。Match ブロックで個別に上書きしている"
            "構成では実挙動が異なる場合があります)</span>")
        self.lbl_state.setWordWrap(True)
        form.addRow(self.lbl_state)

        self.chk_disable_pw = QCheckBox(
            "パスワード認証を無効化する(鍵認証のみにする)")
        self.chk_disable_pw.setEnabled(password_enabled)
        if not password_enabled:
            self.chk_disable_pw.setToolTip("既にパスワード認証は無効です")

        # 複数ポートをわざと設定している場合に備え、現在の待受ポートから
        # 「どれを基準に変更するか」を選ばせる(#62)
        self.cb_cur_port = QComboBox()
        for p in (current_ports or [current_port]):
            self.cb_cur_port.addItem(str(p), p)
        self.cb_cur_port.currentIndexChanged.connect(self._on_cur_port_changed)

        self.chk_change_port = QCheckBox("ポート番号を変更する")
        self.sp_port = QSpinBox()
        self.sp_port.setRange(1, 65535)
        self.sp_port.setValue(current_port)
        self.sp_port.setEnabled(False)
        self.chk_change_port.toggled.connect(self.sp_port.setEnabled)

        form.addRow(self.chk_disable_pw)
        if (current_ports or [current_port]) != [current_port]:
            form.addRow("現在の待受ポート", self.cb_cur_port)
        form.addRow(self.chk_change_port, self.sp_port)

        warn = QLabel(
            "⚠ サーバーの SSH 設定を変更します。安全のため、変更前に設定を"
            "バックアップし、構文検証・疎通確認をします。パスワード認証の無効化は"
            "「登録済みの鍵で実際にログインできること」を確認できた場合のみ実行します。"
            "\nsudo パスワードが必要です。")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{style.WARN};")

        buttons = QDialogButtonBox()
        buttons.addButton("変更を適用", QDialogButtonBox.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(warn)
        root.addWidget(buttons)
        self._current_port = current_port

    def _on_cur_port_changed(self, _index):
        cur = self.cb_cur_port.currentData()
        if cur is not None:
            self._current_port = int(cur)
            if not self.chk_change_port.isChecked():
                self.sp_port.setValue(self._current_port)

    def _validate_accept(self):
        if not self.chk_disable_pw.isChecked() and not self.chk_change_port.isChecked():
            return
        if (self.chk_change_port.isChecked()
                and self.sp_port.value() == self._current_port):
            self.chk_change_port.setChecked(False)
        self.accept()

    def result_settings(self) -> dict:
        return {
            "disable_password": True if self.chk_disable_pw.isChecked() else None,
            "new_port": (self.sp_port.value()
                         if self.chk_change_port.isChecked() else None),
        }


class SettingsDialog(QDialog):
    """アプリ設定。"""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("設定")
        self.setModal(True)
        self.setMinimumWidth(440)
        form = QFormLayout()
        self.chk_sudo = QCheckBox("sudo プロンプトを検知したら送信ボタンを表示")
        self.chk_sudo.setChecked(settings.get("sudo_autofill"))
        self.chk_rclick = QCheckBox("右クリックで貼り付け (PuTTY 流)")
        self.chk_rclick.setChecked(settings.get("right_click_paste"))
        self.chk_override = QCheckBox("新しい接続で権限無視スイッチを既定 ON")
        self.chk_override.setChecked(settings.get("permission_override"))
        self.chk_editor = QCheckBox("テキストファイルは内蔵エディタで開く")
        self.chk_editor.setChecked(settings.get("open_text_in_editor"))
        self.chk_extup = QCheckBox("関連付けアプリで開いたファイルの変更を自動アップロード")
        self.chk_extup.setChecked(settings.get("external_autoupload"))
        self.chk_session_log = QCheckBox("ターミナル受信出力を自動保存 (PuTTY logging 相当)")
        self.chk_session_log.setChecked(settings.get("session_log"))
        log_row = QWidget()
        log_lay = QHBoxLayout(log_row)
        log_lay.setContentsMargins(0, 0, 0, 0)
        self.ed_session_log_dir = QLineEdit(settings.get("session_log_dir") or "")
        self.ed_session_log_dir.setPlaceholderText("空欄で既定の設定ディレクトリ/logs")
        self.ed_session_log_dir.setEnabled(self.chk_session_log.isChecked())
        self.btn_session_log_dir = QPushButton("参照…")
        self.btn_session_log_dir.setEnabled(self.chk_session_log.isChecked())
        self.btn_session_log_dir.clicked.connect(self._browse_session_log_dir)
        self.chk_session_log.toggled.connect(self.ed_session_log_dir.setEnabled)
        self.chk_session_log.toggled.connect(self.btn_session_log_dir.setEnabled)
        log_lay.addWidget(self.ed_session_log_dir, 1)
        log_lay.addWidget(self.btn_session_log_dir)
        self.sp_tfont = QSpinBox()
        self.sp_tfont.setRange(7, 32)
        self.sp_tfont.setValue(settings.get("terminal_font_size"))
        self.sp_efont = QSpinBox()
        self.sp_efont.setRange(7, 32)
        self.sp_efont.setValue(settings.get("editor_font_size"))
        self.sp_tab = QSpinBox()
        self.sp_tab.setRange(1, 8)
        self.sp_tab.setValue(settings.get("editor_tab_width"))
        form.addRow("", self.chk_sudo)
        form.addRow("", self.chk_rclick)
        form.addRow("", self.chk_override)
        form.addRow("", self.chk_editor)
        form.addRow("", self.chk_extup)
        form.addRow("", self.chk_session_log)
        form.addRow("ログ保存先", log_row)
        form.addRow("ターミナル文字サイズ", self.sp_tfont)
        form.addRow("エディタ文字サイズ", self.sp_efont)
        form.addRow("エディタのタブ幅", self.sp_tab)
        note = QLabel("一部の設定は新しい接続/タブから反映されます。")
        note.setStyleSheet(f"color:{style.FG_MUTED};")
        buttons = QDialogButtonBox()
        buttons.addButton("保存", QDialogButtonBox.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(note)
        root.addWidget(buttons)

    def _browse_session_log_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "ログ保存先のディレクトリを選択",
            self.ed_session_log_dir.text() or str(Path.home()))
        if d:
            self.ed_session_log_dir.setText(d)

    def _save(self):
        s = self.settings
        s.set("sudo_autofill", self.chk_sudo.isChecked())
        s.set("right_click_paste", self.chk_rclick.isChecked())
        s.set("permission_override", self.chk_override.isChecked())
        s.set("open_text_in_editor", self.chk_editor.isChecked())
        s.set("external_autoupload", self.chk_extup.isChecked())
        s.set("session_log", self.chk_session_log.isChecked())
        s.set("session_log_dir", self.ed_session_log_dir.text().strip())
        s.set("terminal_font_size", self.sp_tfont.value())
        s.set("editor_font_size", self.sp_efont.value())
        s.set("editor_tab_width", self.sp_tab.value())
        self.accept()
