#!/bin/bash
# Claude Code on the web 用 SessionStart フック。
# 依存を入れて、セッション開始直後から pytest / compileall が動く状態にする。
set -euo pipefail

# リモート(Claude Code on the web)以外では何もしない
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# ヘッドレス Qt(PySide6)に必要なシステムライブラリ(冪等)
if ! ldconfig -p | grep -q libEGL.so.1; then
  export DEBIAN_FRONTEND=noninteractive
  (apt-get update -q || true)
  apt-get install -y -q libegl1 libgl1
fi

# Python 依存(pytest / ruff / PySide6 / paramiko など)
python -m pip install -q -r requirements-dev.txt

# GUI はオフスクリーンで動かす(ヘッドレス環境に X は無い)
echo 'export QT_QPA_PLATFORM=offscreen' >> "$CLAUDE_ENV_FILE"

echo "SessionStart: 依存インストール完了(pytest / compileall / ruff 実行可能)"
