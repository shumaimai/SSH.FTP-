"""ファイルの文脈アクション(Issue #98:「デスクトップ PC のように」)。

SFTP ブラウザの右クリック「実行」サブメニューに出す、ファイル種別ごとの
コマンド定義。**コマンドはターミナルへ入力するだけで自動実行しない**
(Enter は人間が押す。sudo ワンタップ送信・スニペットと同じ安全思想)。

組み込み変数(自動で解決・シェルクォート済み):
  {{path}} = リモートのフルパス / {{dir}} = 親ディレクトリ /
  {{name}} = ファイル名 / {{stem}} = 拡張子なしのファイル名
それ以外の {{変数}} は送信前にダイアログで入力させる(例: {{tag}})。
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .snippets import expand_snippet, find_variables


@dataclass(frozen=True)
class FileAction:
    """「実行」メニューの 1 項目。"""

    label: str
    command: str


def _q(path: str) -> str:
    """POSIX シェル用のクォート(filebrowser と同方式)。"""
    return "'" + path.replace("'", "'\\''") + "'"


_COMPOSE_ACTIONS = [
    FileAction("Compose 起動 (up -d)", "docker compose -f {{path}} up -d"),
    FileAction("Compose 停止 (down)", "docker compose -f {{path}} down"),
    FileAction("Compose 状態 (ps)", "docker compose -f {{path}} ps"),
    FileAction("Compose ログ (直近 50 行)",
               "docker compose -f {{path}} logs --tail 50"),
]

_SERVICE_ACTIONS = [
    FileAction("サービス起動 (systemctl start)", "sudo systemctl start {{name}}"),
    FileAction("サービス停止 (systemctl stop)", "sudo systemctl stop {{name}}"),
    FileAction("サービス再起動 (systemctl restart)",
               "sudo systemctl restart {{name}}"),
    FileAction("サービス状態 (systemctl status)", "systemctl status {{name}}"),
]

# ファイル名の完全一致(小文字)で決まるアクション
_BY_NAME: dict[str, list[FileAction]] = {
    "docker-compose.yml": _COMPOSE_ACTIONS,
    "docker-compose.yaml": _COMPOSE_ACTIONS,
    "compose.yml": _COMPOSE_ACTIONS,
    "compose.yaml": _COMPOSE_ACTIONS,
    "dockerfile": [
        FileAction("イメージをビルド (docker build)",
                   "docker build -t {{tag}} {{dir}}"),
    ],
    "makefile": [
        FileAction("make を実行", "make -C {{dir}}"),
        FileAction("make (ターゲット指定)", "make -C {{dir}} {{target}}"),
    ],
    "package.json": [
        FileAction("npm install", "npm install --prefix {{dir}}"),
        FileAction("npm run (スクリプト指定)",
                   "npm run {{script}} --prefix {{dir}}"),
    ],
    "requirements.txt": [
        FileAction("pip install -r", "pip install -r {{path}}"),
    ],
}

# 拡張子(小文字)で決まるアクション
_BY_EXT: dict[str, list[FileAction]] = {
    "py": [FileAction("Python で実行", "python3 {{path}}")],
    "sh": [FileAction("bash で実行", "bash {{path}}"),
           FileAction("実行権限を付けて実行", "chmod +x {{path}} && {{path}}")],
    "jar": [FileAction("Java で実行 (java -jar)", "java -jar {{path}}")],
    "service": _SERVICE_ACTIONS,
    "sql": [FileAction("MySQL で実行", "mysql {{db}} < {{path}}"),
            FileAction("psql で実行", "psql -d {{db}} -f {{path}}")],
    "tar": [FileAction("展開 (tar xf)", "tar xf {{path}} -C {{dir}}")],
    "gz": [FileAction("展開 (tar xzf)", "tar xzf {{path}} -C {{dir}}")],
    "tgz": [FileAction("展開 (tar xzf)", "tar xzf {{path}} -C {{dir}}")],
    "zip": [FileAction("展開 (unzip)", "unzip {{path}} -d {{dir}}")],
}


def actions_for(name: str) -> list[FileAction]:
    """ファイル名から「実行」メニューに出すアクション一覧を返す(無ければ空)。"""
    base = name.lower()
    if base in _BY_NAME:
        return list(_BY_NAME[base])
    ext = base.rsplit(".", 1)[-1] if "." in base else ""
    return list(_BY_EXT.get(ext, []))


def build_command(action: FileAction, remote_path: str) -> tuple[str, list[str]]:
    """組み込み変数を解決したコマンドと、残りの要入力変数名を返す。

    パス系の変数はシェルクォート済みで埋め込む(空白・記号入りパス対策)。
    """
    name = posixpath.basename(remote_path)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    builtin = {
        "path": _q(remote_path),
        "dir": _q(posixpath.dirname(remote_path) or "/"),
        "name": _q(name),
        "stem": _q(stem),
    }
    cmd = expand_snippet(action.command, builtin)
    return cmd, find_variables(cmd)
