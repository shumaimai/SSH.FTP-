"""ポートフォワード (-L / -R / -D)。

- LocalForward : ローカルポートフォワード (-L)
- RemoteForward: リモートポートフォワード (-R)
- DynamicForward: ダイナミック (SOCKS5) ポートフォワード (-D)

LocalForward / DynamicForward はローカルで待ち受け、リモート側へ SSH 経由で中継する。
RemoteForward はリモート側で待ち受け、ローカル側のホスト:ポートへ中継する。
"""
from __future__ import annotations

import logging
import select
import socket
import struct
import threading

logger = logging.getLogger(__name__)


class ForwardError(Exception):
    """フォワードの起動/停止で発生したエラー。"""


class Forward:
    """フォワード実装の共通インターフェース。"""

    def label(self) -> str:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


def _pump_stream(sock: socket.socket, chan):
    """ソケットと paramiko チャネル間をバイ方向にポンプする。"""
    sock.setblocking(False)
    chan.setblocking(False)
    to_chan = b""
    to_sock = b""

    def _send_ready(obj):
        if hasattr(obj, "send_ready"):
            return obj.send_ready()
        try:
            _, w, _ = select.select([], [obj], [], 0)
        except OSError:
            return False
        return bool(w)

    def _send(obj, data: bytes):
        try:
            return obj.send(data)
        except (BlockingIOError, InterruptedError):
            return 0
        except OSError:
            return None

    def _recv(obj, n: int):
        try:
            return obj.recv(n)
        except (BlockingIOError, InterruptedError):
            return None
        except OSError:
            return b""

    while True:
        # 読み取りは両方 select で待てる。書き込みは real socket だけ select の
        # 書き込みリストに入れられる。paramiko チャネルの fileno() は内部パイプの
        # 読み取り端なので select の書き込みリストでは「書ける」と報告されず、
        # ここに入れると返り(sock→chan)経路が永久に流れない。チャネルの送信可否は
        # send_ready() だけで判断し、直接 send する。
        rlist = [sock, chan]
        wlist = []
        if to_sock and _send_ready(sock):
            wlist.append(sock)

        timeout = 0.1 if (to_chan or to_sock) else 1.0
        try:
            r, w, _ = select.select(rlist, wlist, [], timeout)
        except OSError:
            break

        if sock in w and to_sock:
            sent = _send(sock, to_sock)
            if sent is None or sent == 0:
                break
            to_sock = to_sock[sent:]

        if to_chan and _send_ready(chan):
            sent = _send(chan, to_chan)
            if sent is None:
                break
            if sent:
                to_chan = to_chan[sent:]

        if sock in r:
            data = _recv(sock, 16384)
            if data is None:
                pass
            elif data == b"":
                if not to_chan:
                    break
            else:
                to_chan += data

        if chan in r:
            if getattr(chan, "recv_ready", lambda: True)():
                data = _recv(chan, 16384)
                if data is None:
                    pass
                elif data == b"":
                    if not to_sock:
                        break
                else:
                    to_sock += data
            if getattr(chan, "closed", False) or getattr(chan, "eof_received", False):
                if not to_sock:
                    break


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """ソケットから n バイトをブロッキングで読み込む。"""
    if n <= 0:
        return b""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("接続が切断されました")
        buf += chunk
    return buf


class LocalForward(Forward):
    """1 本のローカルフォワード。start()/stop() で制御。"""

    def __init__(self, transport, local_host: str, local_port: int,
                 remote_host: str, remote_port: int):
        self.transport = transport
        self.local_host = local_host
        self.local_port = local_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.error: str | None = None

    def label(self) -> str:
        return (f"{self.local_host}:{self.local_port} → "
                f"{self.remote_host}:{self.remote_port}")

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.local_host, self.local_port))
        srv.listen(16)
        srv.settimeout(1.0)
        self._server = srv
        # bind(0) で自動割り当てした場合に実ポートを反映
        self.local_port = srv.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while self._running:
            try:
                client, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client, addr),
                             daemon=True).start()

    def _handle(self, client: socket.socket, addr):
        try:
            chan = self.transport.open_channel(
                "direct-tcpip",
                (self.remote_host, self.remote_port),
                client.getpeername(),
            )
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            client.close()
            return
        if chan is None:
            client.close()
            return
        try:
            _pump_stream(client, chan)
        finally:
            try:
                chan.close()
            except Exception:
                logger.debug("フォワードチャネルの close に失敗 (無視)", exc_info=True)
            try:
                client.close()
            except Exception:
                logger.debug("フォワードクライアントソケットの close に失敗 (無視)",
                             exc_info=True)

    def stop(self):
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                logger.debug("フォワードサーバーソケットの close に失敗 (無視)", exc_info=True)
        self._server = None


class RemoteForward(Forward):
    """1 本のリモートポートフォワード (-R)。

    remote_host:remote_port (サーバー側) へ来た接続を、local_host:local_port
    (クライアント側) へ転送する。
    """

    def __init__(self, transport, remote_host: str, remote_port: int,
                 local_host: str, local_port: int):
        self.transport = transport
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_host = local_host
        self.local_port = local_port
        self._handlers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._running = False
        self.error: str | None = None

    def label(self) -> str:
        return (f"{self.remote_host}:{self.remote_port} ← "
                f"{self.local_host}:{self.local_port}")

    def start(self) -> None:
        try:
            port = self.transport.request_port_forward(
                self.remote_host, self.remote_port, self._handler
            )
        except Exception as e:  # noqa: BLE001
            raise ForwardError(
                f"リモートフォワードの要求が拒否されました: {e}"
            ) from e
        if port is not None:
            self.remote_port = port
        self._running = True

    def _handler(self, chan, origin_addr, dest_addr):
        if not self._running:
            try:
                chan.close()
            except Exception:
                pass
            return
        t = threading.Thread(
            target=self._relay, args=(chan, origin_addr), daemon=True
        )
        with self._lock:
            self._handlers.append(t)
        t.start()

    def _relay(self, chan, origin_addr):
        sock = None
        try:
            sock = socket.create_connection(
                (self.local_host, self.local_port), timeout=10
            )
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            logger.warning(
                "リモートフォワード: %s から %s:%s への接続に失敗: %s",
                origin_addr, self.local_host, self.local_port, e
            )
            try:
                chan.close()
            except Exception:
                pass
            return
        try:
            _pump_stream(sock, chan)
        finally:
            try:
                chan.close()
            except Exception:
                logger.debug("リモートフォワードチャネルの close に失敗 (無視)",
                             exc_info=True)
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                logger.debug("リモートフォワードローカルソケットの close に失敗 (無視)",
                             exc_info=True)
            with self._lock:
                try:
                    self._handlers.remove(threading.current_thread())
                except ValueError:
                    pass

    def stop(self):
        self._running = False
        try:
            self.transport.cancel_port_forward(self.remote_host, self.remote_port)
        except Exception:
            logger.debug("cancel_port_forward に失敗 (無視)", exc_info=True)


class DynamicForward(Forward):
    """ダイナミック (SOCKS5) ポートフォワード (-D)。

    ローカルホスト:ポートで SOCKS5 プロキシを待ち受け、接続先を
    direct-tcpip チャネルで SSH 経由に転送する。
    """

    def __init__(self, transport, local_host: str, local_port: int):
        self.transport = transport
        self.local_host = local_host
        self.local_port = local_port
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.error: str | None = None

    def label(self) -> str:
        return f"SOCKS5 {self.local_host}:{self.local_port}"

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.local_host, self.local_port))
        srv.listen(16)
        srv.settimeout(1.0)
        self._server = srv
        self.local_port = srv.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while self._running:
            try:
                client, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client, addr),
                             daemon=True).start()

    def _handle_client(self, client: socket.socket, addr):
        client.settimeout(10.0)
        chan = None
        try:
            # SOCKS5 認証ネゴシエーション(ノーオンリー)
            ver, nmethods = _recv_exact(client, 2)
            if ver != 0x05:
                return
            if nmethods:
                _recv_exact(client, nmethods)
            client.sendall(b"\x05\x00")

            # リクエスト
            ver, cmd, rsv, atyp = _recv_exact(client, 4)
            if ver != 0x05:
                return
            if cmd != 0x01:  # CONNECT のみ
                client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return

            if atyp == 0x01:
                raw = _recv_exact(client, 4)
                host = ".".join(str(b) for b in raw)
            elif atyp == 0x03:
                length = _recv_exact(client, 1)[0]
                host = _recv_exact(client, length).decode("utf-8", "replace")
            elif atyp == 0x04:
                raw = _recv_exact(client, 16)
                host = socket.inet_ntop(socket.AF_INET6, raw)
            else:
                client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return

            port = struct.unpack("!H", _recv_exact(client, 2))[0]

            chan = self.transport.open_channel(
                "direct-tcpip", (host, port), client.getpeername()
            )
            if chan is None:
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                return

            # 成功応答(BND.ADDR は 0.0.0.0:0)
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception as e:  # noqa: BLE001
            logger.warning("SOCKS5 ハンドシェイク失敗: %s", e)
            return
        finally:
            client.settimeout(None)

        try:
            _pump_stream(client, chan)
        finally:
            try:
                if chan is not None:
                    chan.close()
            except Exception:
                logger.debug("SOCKS5 チャネルの close に失敗 (無視)", exc_info=True)
            try:
                client.close()
            except Exception:
                logger.debug("SOCKS5 クライアントソケットの close に失敗 (無視)",
                             exc_info=True)

    def stop(self):
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                logger.debug("SOCKS5 サーバーソケットの close に失敗 (無視)", exc_info=True)
        self._server = None
