"""ダイアログ群。

- DoubleCheckDialog: 破壊的操作(削除/上書き)の 2 段階目。確認語の入力を要求
- HostKeyDialog: ホスト鍵フィンガープリントの確認 (初回 / 変更検出)
- ConnectDialog: 接続プロファイルの新規作成・編集
"""
from __future__ import annotations

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
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .config import AUTH_AGENT, AUTH_KEY, AUTH_PASSWORD, Profile


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
                "<b style='color:#e06c75; font-size:14px;'>"
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
            old.setStyleSheet("color:#888;")
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

    def __init__(self, parent=None, profile: Profile | None = None):
        super().__init__(parent)
        self.setWindowTitle("接続設定")
        self.setModal(True)
        self.setMinimumWidth(460)
        p = profile or Profile()

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

        self.ed_initial = QLineEdit(p.initial_path)
        self.ed_initial.setPlaceholderText("空欄ならホームディレクトリ")

        self.chk_save = QCheckBox("パスワード/パスフレーズを保存する")
        self.chk_save.setChecked(p.save_secrets)
        self.chk_sudo = QCheckBox("sudo のパスワードはログインと同じ")
        self.chk_sudo.setChecked(p.sudo_same_as_password)

        form.addRow("名前", self.ed_name)
        form.addRow("ホスト", self.ed_host)
        form.addRow("ポート", self.sp_port)
        form.addRow("ユーザー名", self.ed_user)
        form.addRow("認証方式", self.cb_auth)
        form.addRow("秘密鍵", key_row)
        form.addRow("初期パス", self.ed_initial)
        form.addRow("", self.chk_save)
        form.addRow("", self.chk_sudo)

        note = QLabel(
            "保存する場合、OS の資格情報ストア(Windows 資格情報マネージャ等)を"
            "優先して使います。利用できない環境では暗号化ファイルに保存します。"
        )
        note.setStyleSheet("color:#888;")
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

        self.cb_auth.currentIndexChanged.connect(self._toggle_key_row)
        self._toggle_key_row()

    def _toggle_key_row(self):
        self._key_row.setEnabled(self.cb_auth.currentData() == AUTH_KEY)

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
            save_secrets=self.chk_save.isChecked(),
            sudo_same_as_password=self.chk_sudo.isChecked(),
        )


class TunnelDialog(QDialog):
    """ローカルポートフォワード (-L) の追加。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ポートフォワード (ローカル -L)")
        self.setModal(True)
        self.setMinimumWidth(420)
        form = QFormLayout()
        self.sp_lport = QSpinBox()
        self.sp_lport.setRange(1, 65535)
        self.sp_lport.setValue(8080)
        self.ed_lhost = QLineEdit("127.0.0.1")
        self.ed_rhost = QLineEdit("127.0.0.1")
        self.sp_rport = QSpinBox()
        self.sp_rport.setRange(1, 65535)
        self.sp_rport.setValue(80)
        form.addRow("ローカル待受ホスト", self.ed_lhost)
        form.addRow("ローカル待受ポート", self.sp_lport)
        form.addRow("転送先ホスト(サーバー側から見て)", self.ed_rhost)
        form.addRow("転送先ポート", self.sp_rport)
        note = QLabel(
            "例: ローカル 8080 → サーバー側から見た 127.0.0.1:80 に転送。\n"
            "localhost:ローカルポート にアクセスすると、SSH 経由で転送先へ繋がります。")
        note.setStyleSheet("color:#888;")
        note.setWordWrap(True)
        buttons = QDialogButtonBox()
        buttons.addButton("追加", QDialogButtonBox.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(note)
        root.addWidget(buttons)

    def result(self):
        return {
            "local_host": self.ed_lhost.text().strip() or "127.0.0.1",
            "local_port": self.sp_lport.value(),
            "remote_host": self.ed_rhost.text().strip() or "127.0.0.1",
            "remote_port": self.sp_rport.value(),
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
        self.chk_sudo = QCheckBox("sudo プロンプトを検知してパスワードを自動入力")
        self.chk_sudo.setChecked(settings.get("sudo_autofill"))
        self.chk_rclick = QCheckBox("右クリックで貼り付け (PuTTY 流)")
        self.chk_rclick.setChecked(settings.get("right_click_paste"))
        self.chk_override = QCheckBox("新しい接続で権限無視スイッチを既定 ON")
        self.chk_override.setChecked(settings.get("permission_override"))
        self.chk_editor = QCheckBox("テキストファイルは内蔵エディタで開く")
        self.chk_editor.setChecked(settings.get("open_text_in_editor"))
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
        form.addRow("ターミナル文字サイズ", self.sp_tfont)
        form.addRow("エディタ文字サイズ", self.sp_efont)
        form.addRow("エディタのタブ幅", self.sp_tab)
        note = QLabel("一部の設定は新しい接続/タブから反映されます。")
        note.setStyleSheet("color:#888;")
        buttons = QDialogButtonBox()
        buttons.addButton("保存", QDialogButtonBox.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(note)
        root.addWidget(buttons)

    def _save(self):
        s = self.settings
        s.set("sudo_autofill", self.chk_sudo.isChecked())
        s.set("right_click_paste", self.chk_rclick.isChecked())
        s.set("permission_override", self.chk_override.isChecked())
        s.set("open_text_in_editor", self.chk_editor.isChecked())
        s.set("terminal_font_size", self.sp_tfont.value())
        s.set("editor_font_size", self.sp_efont.value())
        s.set("editor_tab_width", self.sp_tab.value())
        self.accept()
