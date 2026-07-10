"""LocalForward のテスト。

- ユニット: フェイク Transport(open_channel が実ソケットを返す)でポンプ部を検証。
  ネットワーク不要なので CI で常に走る。
- 結合(ライブ): 実 sshd がある環境でのみ実行(HASHI_LIVE_SSH=1)。
  Issue #1 の実機検証を再現可能にしたもの。
"""
from __future__ import annotations

import os
import select
import socket
import socketserver
import threading
import time

import pytest

from hashi.forward import LocalForward


class FakeChannel:
    """paramiko.Channel の代わり。実ソケットを direct-tcpip チャネルに見立てる。"""

    def __init__(self, sock: socket.socket):
        self._s = sock
        self.closed = False
        self.eof_received = False

    def fileno(self):
        return self._s.fileno()

    def setblocking(self, flag):
        self._s.setblocking(flag)

    def sendall(self, data):
        self._s.sendall(data)

    def recv_ready(self):
        if self.closed:
            return False
        r, _, _ = select.select([self._s], [], [], 0)
        return bool(r)

    def recv(self, n):
        data = self._s.recv(n)
        if not data:
            self.eof_received = True
        return data

    def close(self):
        self.closed = True
        try:
            self._s.close()
        except OSError:
            pass


class FakeTransport:
    """open_channel でリモート先へ TCP 接続するだけの Transport もどき。"""

    def __init__(self, fail: bool = False):
        self.fail = fail

    def open_channel(self, kind, dest_addr, src_addr):
        assert kind == "direct-tcpip"
        if self.fail:
            raise OSError("Connection refused: Connect failed")
        return FakeChannel(socket.create_connection(dest_addr, timeout=5))


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            self.request.sendall(data)


@pytest.fixture()
def echo_server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _connect_forward(fwd: LocalForward) -> socket.socket:
    return socket.create_connection(("127.0.0.1", fwd.local_port), timeout=5)


def test_forward_roundtrip(echo_server):
    """bind(0) の実ポート反映と、トンネル越しの往復。"""
    host, port = echo_server
    fwd = LocalForward(FakeTransport(), "127.0.0.1", 0, host, port)
    fwd.start()
    try:
        assert fwd.local_port != 0
        with _connect_forward(fwd) as c:
            c.sendall(b"hello-tunnel")
            assert _recv_exact(c, len(b"hello-tunnel")) == b"hello-tunnel"
    finally:
        fwd.stop()


def test_forward_large_payload(echo_server):
    """まとまったサイズ(1MB)がバイト単位で一致して返る。"""
    host, port = echo_server
    payload = os.urandom(1024 * 1024)
    fwd = LocalForward(FakeTransport(), "127.0.0.1", 0, host, port)
    fwd.start()
    try:
        with _connect_forward(fwd) as c:
            threading.Thread(target=c.sendall, args=(payload,), daemon=True).start()
            assert _recv_exact(c, len(payload)) == payload
    finally:
        fwd.stop()


def test_forward_concurrent_connections(echo_server):
    """複数クライアントの同時接続がそれぞれ独立して通る。"""
    host, port = echo_server
    fwd = LocalForward(FakeTransport(), "127.0.0.1", 0, host, port)
    fwd.start()
    errors: list[str] = []

    def worker(i: int):
        try:
            msg = f"msg-{i}".encode() * 100
            with _connect_forward(fwd) as c:
                c.sendall(msg)
                if _recv_exact(c, len(msg)) != msg:
                    errors.append(f"mismatch {i}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{i}: {e}")

    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert not errors, errors
    finally:
        fwd.stop()


def test_forward_open_channel_failure():
    """リモート側に到達できない場合、クライアントは切断されエラーが記録される。"""
    fwd = LocalForward(FakeTransport(fail=True), "127.0.0.1", 0, "127.0.0.1", 1)
    fwd.start()
    try:
        with _connect_forward(fwd) as c:
            # サーバー側が close するので、read は EOF か RST になる
            c.settimeout(5)
            try:
                assert c.recv(1) == b""
            except OSError:
                pass
        for _ in range(50):
            if fwd.error:
                break
            time.sleep(0.1)
        assert fwd.error and "refused" in fwd.error.lower()
    finally:
        fwd.stop()


def test_forward_stop_closes_listener(echo_server):
    """stop() 後は待ち受けポートに接続できない。"""
    host, port = echo_server
    fwd = LocalForward(FakeTransport(), "127.0.0.1", 0, host, port)
    fwd.start()
    fwd.stop()
    time.sleep(1.2)  # accept ループの timeout(1s)を跨ぐ
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", fwd.local_port), timeout=2)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    sock.settimeout(15)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            break
        buf += chunk
    return buf


# ---- ライブ結合テスト(実 sshd が必要。Issue #1 の実機検証を再現) ----------
#
# 実行方法:
#   sshd を 127.0.0.1:2222 で起動し、tester/testpass を用意した上で
#   HASHI_LIVE_SSH=1 QT_QPA_PLATFORM=offscreen pytest tests/test_forward.py -k live

@pytest.mark.skipif(os.environ.get("HASHI_LIVE_SSH") != "1",
                    reason="実 sshd が必要(HASHI_LIVE_SSH=1 で有効化)")
def test_forward_live_real_sshd(tmp_path, echo_server):
    from hashi.config import AUTH_PASSWORD, KnownHosts, Profile
    from hashi.ssh_core import SshSession

    class Ui:
        def get_secret(self, prompt):
            return os.environ.get("HASHI_LIVE_PASS", "testpass")

        def confirm_hostkey(self, info):
            return True

    host, port = echo_server
    prof = Profile(name="live", host="127.0.0.1",
                   port=int(os.environ.get("HASHI_LIVE_PORT", "2222")),
                   username=os.environ.get("HASHI_LIVE_USER", "tester"),
                   auth_method=AUTH_PASSWORD)
    sess = SshSession(prof, KnownHosts(path=tmp_path / "kh.json"))
    sess.connect(Ui())
    fwd = LocalForward(sess.transport, "127.0.0.1", 0, host, port)
    fwd.start()
    try:
        with _connect_forward(fwd) as c:
            c.sendall(b"live-tunnel")
            assert _recv_exact(c, len(b"live-tunnel")) == b"live-tunnel"
    finally:
        fwd.stop()
        sess.close()
