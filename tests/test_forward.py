"""ポートフォワード (-L / -R / -D) のテスト。

- ユニット: フェイク Transport(open_channel/request_port_forward を模倣)で各フォワードの
  起動・ポンプ・停止を検証。ネットワーク不要なので CI で常に走る。
- 結合(ライブ): 実 sshd がある環境でのみ実行(HASHI_LIVE_SSH=1)。
  Issue #1 の実機検証を再現可能にしたもの。
"""
from __future__ import annotations

import os
import select
import socket
import socketserver
import struct
import threading
import time

import pytest

from hashi.forward import DynamicForward, LocalForward, RemoteForward


class FakeChannel:
    """paramiko.Channel の代わり。実ソケットを SSH チャネルに見立てる。"""

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

    def send(self, data):
        return self._s.send(data)

    def send_ready(self):
        if self.closed:
            return False
        try:
            _, w, _ = select.select([], [self._s], [], 0)
        except OSError:
            return False
        return bool(w)

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


class _RemoteHandler(socketserver.BaseRequestHandler):
    """リモートフォワード用サーバー: 接続が来たら FakeTransport に登録されたハンドラへ渡す。"""

    def handle(self):
        transport = self.server.transport
        if transport._handler is None:
            return
        chan = FakeChannel(self.request)
        transport._handler(chan, self.client_address, self.server.server_address)


class _RemoteServer(socketserver.ThreadingTCPServer):
    """リモートフォワード用サーバー: ソケットは handler が閉じる。"""

    def shutdown_request(self, request):
        pass

    def close_request(self, request):
        pass


class FakeTransport:
    """open_channel / request_port_forward / cancel_port_forward を模倣する Transport。"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self._handler = None
        self._remote_server: socketserver.ThreadingTCPServer | None = None

    def open_channel(self, kind, dest_addr, src_addr):
        assert kind == "direct-tcpip"
        if self.fail:
            raise OSError("Connection refused: Connect failed")
        return FakeChannel(socket.create_connection(dest_addr, timeout=5))

    def request_port_forward(self, address, port, handler):
        """サーバー側で listen するポートを確保し、接続時に handler を呼ぶ。"""
        self._handler = handler
        srv = _RemoteServer(("127.0.0.1", 0), _RemoteHandler)
        srv.daemon_threads = True
        srv.transport = self
        self._remote_server = srv
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv.server_address[1]

    def cancel_port_forward(self, address, port):
        if self._remote_server is not None:
            self._remote_server.shutdown()
            self._remote_server.server_close()
            self._remote_server = None


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            while True:
                data = self.request.recv(4096)
                if not data:
                    return
                self.request.sendall(data)
        except OSError:
            pass


@pytest.fixture()
def echo_server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    sock.settimeout(15)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            break
        buf += chunk
    return buf


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


# ---- リモートフォワード (-R) ------------------------------------------------


def _wait_for_remote_forward(fwd: RemoteForward, timeout: float = 5.0) -> bool:
    """リモート側の待受ポートが接続可能になるまで待つ。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", fwd.remote_port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


class _PipeChannel:
    """paramiko チャネルの select 特性を再現するフェイク。

    本物の paramiko チャネルの fileno() は内部パイプの読み取り端なので、
    「データ到着で読み取り可能」にはなるが「書き込みリストで select しても
    書き込み可能にはならない」。FakeChannel は実ソケットを使うためこの差が
    出ず、_pump_stream が chan を select の書き込みリストに入れていたバグを
    見逃していた。このフェイクは fileno にパイプ読み取り端を使うことで、
    返り(sock→chan)経路が send_ready() 経由で正しく流れることを検証する。
    """

    def __init__(self):
        self._r, self._w = os.pipe()
        self._inbox = bytearray()   # recv() で返すデータ
        self.outbox = bytearray()   # send() されたデータ(検証用)
        self.closed = False
        self.eof_received = False
        self._lock = threading.Lock()

    def feed(self, data: bytes):
        with self._lock:
            self._inbox += data
        os.write(self._w, b"\x00")  # 読み取り可能シグナル

    def fileno(self):
        return self._r

    def setblocking(self, flag):
        pass

    def recv_ready(self):
        r, _, _ = select.select([self._r], [], [], 0)
        return bool(r)

    def recv(self, n):
        try:
            os.read(self._r, 1)  # シグナルを1つ消費
        except OSError:
            pass
        with self._lock:
            data = bytes(self._inbox[:n])
            del self._inbox[:n]
        return data

    def send_ready(self):
        return not self.closed

    def send(self, data):
        self.outbox += data
        return len(data)

    def close(self):
        self.closed = True
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass


def test_pump_stream_return_path_via_send_ready():
    """返り(sock→chan)経路が、select 書き込み不可なチャネルでも流れる。

    回帰: _pump_stream が chan を select の書き込みリストに入れていたため、
    paramiko チャネル(fileno が書き込み select 不可)では応答が返らなかった。
    """
    from hashi.forward import _pump_stream

    a, b = socket.socketpair()  # a=sock 側、b=テストの相手
    chan = _PipeChannel()
    th = threading.Thread(target=_pump_stream, args=(a, chan), daemon=True)
    th.start()
    try:
        # sock 側(b)へ「応答」を書く → pump が読み取り to_chan へ → chan.send へ
        b.sendall(b"RESPONSE-BYTES")
        deadline = time.time() + 5
        while bytes(chan.outbox) != b"RESPONSE-BYTES" and time.time() < deadline:
            time.sleep(0.05)
        assert bytes(chan.outbox) == b"RESPONSE-BYTES"

        # 往路(chan→sock)も流れる
        chan.feed(b"REQUEST-BYTES")
        assert _recv_exact(b, len(b"REQUEST-BYTES")) == b"REQUEST-BYTES"
    finally:
        chan.close()
        b.close()
        a.close()


def test_remote_forward_roundtrip(echo_server):
    """リモート待受ポートへ接続すると、ローカル転送先と往復する。"""
    host, port = echo_server
    fwd = RemoteForward(FakeTransport(), "127.0.0.1", 0, host, port)
    fwd.start()
    try:
        assert fwd.remote_port != 0
        assert _wait_for_remote_forward(fwd)
        with socket.create_connection(("127.0.0.1", fwd.remote_port), timeout=5) as c:
            c.sendall(b"hello-remote")
            assert _recv_exact(c, len(b"hello-remote")) == b"hello-remote"
    finally:
        fwd.stop()


def test_remote_forward_stop_closes_listener():
    """stop() 後はリモート待受ポートに接続できない。"""
    fwd = RemoteForward(FakeTransport(), "127.0.0.1", 0, "127.0.0.1", 1)
    fwd.start()
    assert fwd.remote_port != 0
    _wait_for_remote_forward(fwd)
    fwd.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", fwd.remote_port), timeout=2)


# ---- ダイナミックフォワード (-D / SOCKS5) -----------------------------------


def _socks5_request(sock: socket.socket, host: str, port: int, atyp: int = 0x01):
    """SOCKS5 ハンドシェイクと CONNECT リクエストを送信、成功応答を読む。"""
    sock.sendall(b"\x05\x01\x00")
    resp = _recv_exact(sock, 2)
    assert resp == b"\x05\x00"
    if atyp == 0x01:
        addr = socket.inet_aton(host)
    elif atyp == 0x03:
        addr = len(host).to_bytes(1, "big") + host.encode("utf-8")
    else:
        raise ValueError("未対応の atyp")
    req = b"\x05\x01\x00" + atyp.to_bytes(1, "big") + addr + struct.pack("!H", port)
    sock.sendall(req)
    reply = _recv_exact(sock, 10)
    assert reply[:4] == b"\x05\x00\x00\x01"


def test_dynamic_forward_roundtrip(echo_server):
    """SOCKS5 プロキシ経由で接続すると、echo サーバーと往復する。"""
    host, port = echo_server
    fwd = DynamicForward(FakeTransport(), "127.0.0.1", 0)
    fwd.start()
    try:
        with socket.create_connection(("127.0.0.1", fwd.local_port), timeout=5) as c:
            _socks5_request(c, "127.0.0.1", port, atyp=0x01)
            c.sendall(b"hello-dynamic")
            assert _recv_exact(c, len(b"hello-dynamic")) == b"hello-dynamic"
    finally:
        fwd.stop()


def test_dynamic_forward_domain(echo_server):
    """SOCKS5 のドメイン型(ATYP=0x03)リクエストも転送できる。"""
    host, port = echo_server
    fwd = DynamicForward(FakeTransport(), "127.0.0.1", 0)
    fwd.start()
    try:
        with socket.create_connection(("127.0.0.1", fwd.local_port), timeout=5) as c:
            _socks5_request(c, "127.0.0.1", port, atyp=0x03)
            c.sendall(b"hello-domain")
            assert _recv_exact(c, len(b"hello-domain")) == b"hello-domain"
    finally:
        fwd.stop()


def test_dynamic_forward_stop_closes_listener():
    """stop() 後は SOCKS5 待受ポートに接続できない。"""
    fwd = DynamicForward(FakeTransport(), "127.0.0.1", 0)
    fwd.start()
    fwd.stop()
    time.sleep(1.2)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", fwd.local_port), timeout=2)


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
