import paramiko

from hashi.keygen import generate_key, public_key_line, register_public_key


def test_generate_ed25519_with_passphrase(tmp_path):
    path = tmp_path / "id_ed25519"
    key, public_line = generate_key(
        "ed25519", passphrase="秘密", comment="テスト", path=path
    )

    assert isinstance(key, paramiko.Ed25519Key)
    assert public_line.startswith("ssh-ed25519 ")
    assert public_line.endswith(" テスト")
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = paramiko.Ed25519Key.from_private_key_file(str(path), password="秘密")
    assert loaded.get_base64() == key.get_base64()


def test_generate_ecdsa_and_rsa(tmp_path):
    ecdsa, ecdsa_line = generate_key("ecdsa", bits=256, path=tmp_path / "ecdsa")
    rsa, rsa_line = generate_key("rsa", bits=2048, path=tmp_path / "rsa")

    assert isinstance(ecdsa, paramiko.ECDSAKey)
    assert ecdsa_line.startswith("ecdsa-sha2-nistp256 ")
    assert isinstance(rsa, paramiko.RSAKey)
    assert rsa_line.startswith("ssh-rsa ")


def test_public_key_line_omits_empty_comment():
    key, _ = generate_key("ed25519")

    assert public_key_line(key).count(" ") == 1
    assert public_key_line(key, "  host  ").endswith(" host")


class _RemoteFile:
    def __init__(self, sftp, path, mode):
        self.sftp = sftp
        self.path = path
        self.mode = mode

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.sftp.files.get(self.path, b"")

    def write(self, content):
        self.sftp.files[self.path] = content


class _FakeSftp:
    def __init__(self, files=None):
        self.files = files or {}
        self.directories = set()
        self.modes = {}

    def stat(self, path):
        if path not in self.directories:
            raise OSError(2, "not found")
        return object()

    def mkdir(self, path, mode=0o777):
        self.directories.add(path)
        self.modes[path] = mode

    def chmod(self, path, mode):
        self.modes[path] = mode

    def open(self, path, mode):
        if "r" in mode and path not in self.files:
            raise OSError(2, "not found")
        return _RemoteFile(self, path, mode)

    def close(self):
        pass


class _FakeSession:
    def __init__(self, sftp):
        self.sftp = sftp

    def exec_command(self, command):
        assert command == 'printf "%s" "$HOME"'
        return 0, "/home/tester\n", ""

    def open_sftp(self):
        return self.sftp


def test_register_public_key_appends_and_avoids_duplicate():
    key, _ = generate_key("ed25519")
    line = public_key_line(key, "test")
    sftp = _FakeSftp()
    session = _FakeSession(sftp)

    assert register_public_key(session, line) is True
    path = "/home/tester/.ssh/authorized_keys"
    assert sftp.files[path].decode().splitlines() == [line]
    assert sftp.modes["/home/tester/.ssh"] == 0o700
    assert sftp.modes[path] == 0o600
    assert register_public_key(session, line + " changed-comment") is False
    assert sftp.files[path].decode().splitlines() == [line]
