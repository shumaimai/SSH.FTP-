"""テスト共通のフィクスチャとフェイク SSH。"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(scope="session")
def qapp():
    """オフスクリーンの QApplication(GUI 依存テスト用)。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """config_dir を一時ディレクトリへ差し替える。"""
    import hashi.config as cfg

    def fake_dir():
        return tmp_path

    monkeypatch.setattr(cfg, "config_dir", fake_dir)
    # credentials も自前で import 済みの参照を持つため差し替える
    import hashi.credentials as creds
    monkeypatch.setattr(creds, "config_dir", fake_dir, raising=False)
    return tmp_path


class FakeStat:
    def __init__(self, mode):
        self.st_mode = mode


class FakeSFTP:
    """最小限の SFTP。owned に無いパスの chmod は EACCES を投げる。"""

    def __init__(self, modes, owned=()):
        self.modes = dict(modes)
        self.owned = set(owned)
        self.chmods = []

    def stat(self, p):
        if p not in self.modes:
            raise IOError(2, "No such file")
        return FakeStat(self.modes[p])

    def chmod(self, p, m):
        if p not in self.owned:
            raise IOError(13, "Permission denied")
        self.chmods.append((p, m))
        self.modes[p] = 0o100000 | m

    def listdir(self, p):
        return []

    def close(self):
        pass


class FakeSession:
    def __init__(self, sftp, sudo_ok=False, sudo_pw="testpass"):
        self._sftp = sftp
        self.sudo_ok = sudo_ok
        self._pw = sudo_pw
        self.sudo_calls = []

    def open_sftp(self):
        return self._sftp

    def run_sudo(self, cmd, pw):
        self.sudo_calls.append((cmd, pw))
        if self.sudo_ok and pw == self._pw:
            # chmod を反映
            import re
            m = re.match(r"chmod (\d+) (.+)", cmd)
            if m and hasattr(self._sftp, "modes"):
                self._sftp.modes[m.group(2)] = 0o100000 | int(m.group(1), 8)
            return (0, "", "")
        return (1, "", "incorrect password attempt")


@pytest.fixture
def fakes():
    return {"FakeSFTP": FakeSFTP, "FakeSession": FakeSession}
