# 変更履歴

このプロジェクトは [Semantic Versioning](https://semver.org/lang/ja/) に緩く従います。

## [Unreleased]
### 修正
- 内蔵エディタの検索(Ctrl+F / F3 / 次へ / 前へ)が `AttributeError` でクラッシュしていた
  (`QPlainTextEdit.FindFlag` → 正しくは `QTextDocument.FindFlag`)。

### 追加
- **`~/.ssh/config` の読み込み**(#3): ホスト欄の Host エイリアスから HostName / User /
  Port / IdentityFile を解決して接続。ProxyJump / ProxyCommand は未対応で、検出時は
  黙って直接接続せずエラーで明示する。
- ローカルポートフォワード(-L)を実 sshd で通し検証し、`tests/test_forward.py` に
  ユニット + ライブ結合テストとして恒久化(#1)。
- terminal / editor のテスト拡充(選択コピー・全角幅・プロンプト重複抑止・改行変換・
  タイトル通知 / 言語判定・ハイライト・検索回り込み・保存フロー)(#7)。

## [0.2.0] - 2026-07-10
### 追加
- 認証情報の保存(パスワード / パスフレーズ / sudo パスワード)。keyring(Windows 資格情報マネージャ等)優先、無ければ Fernet 暗号化ファイルにフォールバック。
- sudo プロンプトの自動検知とパスワード自動入力(設定で ON/OFF、誤検知時は自動送信を停止)。
- ターミナルの右クリック貼り付け(PuTTY 流。Shift+右クリックでメニュー)。
- **SFTP 権限無視スイッチ**: 権限で弾かれたら一時的に読み書き可にして操作し、直後に元へ戻す。所有者でなければ `sudo chmod` にフォールバック。
- 権限無視の**ジャーナル**: 緩める前に元の権限を fsync 記録し、クラッシュ後は次回接続で復元。pid 生存判定で他セッションの誤爆を防止。復元に sudo が要る場合は促す。
- **内蔵コードエディタ**: 行番号・シンタックスハイライト・検索。Ctrl+S でリモートへ保存。
- ローカルポートフォワード(-L)。
- 設定ダイアログ、セッションメニュー(ポートフォワード、保存パスワード削除)。

### 変更
- バージョンを `hashi/__init__.py` に一元化。

## [0.1.0] - 2026-07-09
### 追加
- 初版。xterm-256color 対話シェル(pyte + 自前描画、日本語 IME、全角描画、選択即コピー、スクロールバック)。
- 公開鍵 / エージェント / パスワード認証、SHA256 フィンガープリントによる TOFU ホスト鍵検証。
- エクスプローラ風 SFTP ブラウザ(D&D アップロード、nav/xfer の 2 チャネル)。
- 削除・上書きの 2 段階確認。
- CLI 接続診断ツール `tools/doctor.py`。

[Unreleased]: https://github.com/shumaimai/SSH.FTP-/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/shumaimai/SSH.FTP-/releases/tag/v0.2.0
[0.1.0]: https://github.com/shumaimai/SSH.FTP-/releases/tag/v0.1.0
