"""ローカルポートフォワード (ssh -L 相当)。

local_host:local_port へ来た接続を、SSH 経由で remote_host:remote_port へ
中継する。一般的な SSH クライアントに必ずある機能。
"""
from __future__ import annotations

import select
import socket
import threading


class LocalForward:
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
            self._pump(client, chan)
        finally:
            try:
                chan.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _pump(sock: socket.socket, chan):
        sock.setblocking(False)
        chan.setblocking(False)
        while True:
            r, _, _ = select.select([sock, chan], [], [], 1.0)
            if sock in r:
                try:
                    data = sock.recv(16384)
                except (BlockingIOError, InterruptedError):
                    data = b""
                except OSError:
                    break
                if data:
                    chan.sendall(data)
                elif data == b"":
                    try:
                        if sock.recv(1, socket.MSG_PEEK) == b"":
                            break
                    except (BlockingIOError, InterruptedError):
                        pass
                    except OSError:
                        break
            if chan in r:
                if chan.recv_ready():
                    data = chan.recv(16384)
                    if not data:
                        break
                    sock.sendall(data)
                if chan.closed or chan.eof_received:
                    break

    def stop(self):
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
        self._server = None
