"""接続診断ツール (GUI なし)。

うまく接続できないときの切り分け用:
  python tools/doctor.py <host> <user> [--port 22] [--key PATH | --password | --agent]

TCP 接続 → SSH ネゴシエーション → ホスト鍵 → 認証 → SFTP → シェル
の順に確認して ✓ / ✗ を表示する。
"""
from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hashi.config import Profile, KnownHosts, AUTH_KEY, AUTH_PASSWORD, AUTH_AGENT  # noqa: E402
from hashi.ssh_core import SshSession, ConnectCancelled, ConnectError  # noqa: E402


class ConsoleUI:
    """ssh_core が要求する ui コールバックのコンソール版。"""

    def __init__(self, auto_yes: bool = False, secret: str | None = None):
        self.auto_yes = auto_yes
        self.secret = secret

    def get_secret(self, prompt: str):
        if self.secret is not None:
            return self.secret
        try:
            return getpass.getpass(prompt.replace("\n", " ") + ": ")
        except (EOFError, KeyboardInterrupt):
            return None

    def confirm_hostkey(self, info: dict) -> bool:
        status = "初回接続" if info["status"] == "new" else "!! 鍵が変更されています !!"
        print(f"  ホスト鍵 [{status}] {info['key_type']} {info['fingerprint']}")
        if self.auto_yes:
            return True
        try:
            return input("  信頼しますか? [y/N]: ").strip().lower() == "y"
        except (EOFError, KeyboardInterrupt):
            return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Hashi 接続診断")
    ap.add_argument("host")
    ap.add_argument("user")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--key", default="", help="秘密鍵ファイルのパス")
    ap.add_argument("--password", action="store_true", help="パスワード認証を使う")
    ap.add_argument("--agent", action="store_true", help="SSH エージェント認証を使う")
    ap.add_argument("--yes", action="store_true", help="ホスト鍵を自動で信頼(検証用)")
    ap.add_argument("--secret", default=None, help=argparse.SUPPRESS)  # テスト用
    args = ap.parse_args()

    if args.agent:
        method = AUTH_AGENT
    elif args.password or not args.key:
        method = AUTH_PASSWORD
    else:
        method = AUTH_KEY

    profile = Profile(
        host=args.host, port=args.port, username=args.user,
        auth_method=method, key_path=args.key,
    )
    print(f"[1] 接続先: {args.user}@{args.host}:{args.port}  認証: {method}")

    session = SshSession(profile, KnownHosts())
    ui = ConsoleUI(auto_yes=args.yes, secret=args.secret)
    t0 = time.time()
    try:
        session.connect(ui)
    except ConnectCancelled:
        print("✗ キャンセルされました")
        return 1
    except ConnectError as e:
        print(f"✗ 接続失敗: {e}")
        return 1
    print(f"✓ [2] 接続 + ホスト鍵検証 + 認証 OK ({time.time() - t0:.2f}s)")

    try:
        sftp = session.open_sftp()
        home = sftp.normalize(".")
        names = sftp.listdir(home)
        print(f"✓ [3] SFTP OK  ホーム: {home} ({len(names)} 項目)")
        sftp.close()
    except Exception as e:  # noqa: BLE001
        print(f"✗ [3] SFTP 失敗: {e}")

    try:
        ch = session.open_shell(80, 24)
        time.sleep(1.0)
        data = ch.recv(4096) if ch.recv_ready() else b""
        print(f"✓ [4] 対話シェル OK (初期出力 {len(data)} bytes)")
        ch.close()
    except Exception as e:  # noqa: BLE001
        print(f"✗ [4] シェル失敗: {e}")

    session.close()
    print("診断終了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
