# Hashi — SSH / SFTP クライアント

![CI](https://github.com/shumaimai/SSH.FTP-/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/shumaimai/SSH.FTP-?sort=semver)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

橋 (bridge)。ローカルとリモートをつなぐ、Windows でまともに使える SSH ターミナル + SFTP ファイルブラウザ.
PuTTY + WinSCP を別々に開かなくていいのがコンセプト。起動すると接続先を選ぶランチャーが開き、
接続すると 1 接続 = 1 ウィンドウでターミナルとファイル操作が横に並ぶ。

## 起動

```
pip install -r requirements.txt
python main.py
```

Python 3.10+ 推奨。exe 化は `pyinstaller --noconsole --name Hashi main.py`。

## 主な機能

### ターミナル
- xterm-256color 対話シェル。256色/truecolor、太字/下線/反転、スクロールバック 5000 行
- 日本語 IME 入力、全角文字の正しい描画、PTY リサイズ追従
- **選択したら自動コピー / 右クリックで貼り付け**(PuTTY 流)。Shift+右クリックでメニュー
- **sudo ワンタップ送信**: `[sudo] password for ...` などのプロンプトを検知すると **「🔑 sudo パスワードを送信」ボタン**が現れ、ワンタップで保存済みパスワードを送信(設定で ON/OFF)。リモート側はプロンプトを偽装できるため**確認なしの自動送信はしない**。送信直後に同じプロンプトが再表示された場合(=パスワード違い)はボタンを出さず手動へ切替

### 認証と保存
- 公開鍵 (Ed25519 / ECDSA / RSA、パスフレーズ対応)、SSH エージェント、パスワード認証
- **パスワード / パスフレーズ / sudo パスワードを保存**できる。保存先は OS の資格情報ストア(Windows 資格情報マネージャ = DPAPI 保護)を優先。使えない環境では設定フォルダに Fernet 暗号化して保存
- ホスト鍵は SHA256 フィンガープリントで TOFU 検証。鍵変更時は警告し `trust` 入力を要求
- **SSH 鍵の生成**(「セッション」/ ランチャーのメニュー): Ed25519 / ECDSA / RSA を生成し、接続中サーバーの `authorized_keys` へ公開鍵を自動登録できる

### SFTP ファイルブラウザ
- エクスプローラ風。フォルダ移動、D&D アップロード(フォルダごと再帰)、F2/Del/F5/Backspace、隠しファイル切替、列ソート
- **削除・上書きは 2 段階確認**(一覧を見せた上で `delete` / `overwrite` と打たせる)
- 転送用と操作用で SFTP チャネルを分離。大きい転送中もブラウズ可能
- **ターミナル連携**: Shift+右クリックから「リモートパスをコピー」「ターミナルでこのディレクトリへ移動 (cd)」「ターミナルにパスを挿入」

### 🔓 権限無視スイッチ(このソフト独自)
ツールバーの「🔓 権限無視」を ON にすると、**権限で弾かれたファイルを一時的に読み書き可能にして操作し、終わったら即座に元の権限へ戻す**。

- 読み取り: 拒否されたら一時的に読取ビット付与 → DL/閲覧 → 復元
- 書き込み: 権限不足なら対象ファイル(新規なら親ディレクトリ)へ一時的に書込ビット付与 → 保存 → 復元
- 自分が所有者でないファイルは SSH 側で **`sudo chmod`** を使って変更(合体ソフトなので同一接続で完結)。sudo パスワードは保存済みを使用
- 付けた権限は操作直後に必ず元へ戻す(異常時もベストエフォートで復元)
- **ジャーナルによるクラッシュ復元**: 権限を緩める **前** に「元の権限」をディスクへ fsync 記録する。もしプロセスが強制終了(kill -9 / 電源断)されて復元できなくても、次回同じサーバーへ接続したときにジャーナルを読んで元へ戻す。各記録には実行元プロセスの pid を持たせ、復元対象は「その pid がもう生きていない=過去にクラッシュしたもの」だけに限定するので、同じサーバーへ同時接続中の別セッションが今まさに緩めている最中のファイルを誤って戻す事故は起きない
- **復元にも権限が要る点への対処**: ジャーナル自体はクライアント側のローカル JSON(自分の設定フォルダ)で、読み書きに特権は不要。ただし root 所有ファイルを元に戻す chmod には結局 sudo が要る。起動時に保存済み sudo パスワードがあればそれで自動復元し、無くて戻せないものが残った場合は「前回緩んだ権限が N 件戻せていません。sudo で戻しますか?」と促す。復元は深いパスから順に行い、親ディレクトリの実行ビットを先に外して子へ辿れなくなる事態を避ける

> 実サーバー検証済み: root 所有 mode 000 のファイルを権限無視で読取 → mode 0 に復元、
> root 所有ディレクトリへの新規作成 / root 所有 644 ファイルの上書き → それぞれ元の権限へ復元。
> ジャーナルは参照カウント・pid 生存判定・クラッシュ復元を単体テストで検証済み。

### 📝 内蔵エディタ
- テキストファイルはダブルクリックで**内蔵エディタ**が開く(メモ帳ではない)
- 行番号、現在行ハイライト、シンタックスハイライト(Python / C 系 / JS / シェル / 設定)、検索(Ctrl+F / F3)
- **Ctrl+S でそのままサーバーへ保存**。権限が足りなくても権限無視スイッチが ON なら自動対処
- バイナリ / 大きいファイルは従来どおり関連付けアプリで開く

### 一般的な SSH クライアント機能
- **ポートフォワード**: 「セッション」メニューから追加。ローカル (-L)、リモート (-R)、ダイナミック (-D / SOCKS5) に対応
- **ProxyJump(踏み台経由の多段接続)**: 接続設定の「踏み台」欄または `~/.ssh/config` の ProxyJump で指定(`[user@]host[:port]`、カンマ区切りで多段)。踏み台ごとにホスト鍵確認と認証を行い、接続先の保存パスワードを踏み台に流用しない
- **`~/.ssh/config` の Host エイリアス**: ホスト欄にエイリアス名を書くと HostName / User / Port / IdentityFile / ProxyJump を解決して接続(ProxyCommand は未対応で、検出時は黙って直接接続せずエラーで明示)
- **1 接続 = 1 ウィンドウ + サーバー選択ランチャー**: 起動画面で接続先を選び、接続すると独立したウィンドウが開く。プロファイル / 既知ホスト / 認証情報 / 設定は全ウィンドウで共有
- プロファイル保存、接続診断 `tools/doctor.py`

### 接続情報の持ち運び・共有
- **書き出し / 読み込み**(ファイルメニュー): プロファイル一式 + known_hosts を 1 ファイルへエクスポート/インポート。パスワード等の秘密情報は既定で含めず、含める場合はパスフレーズ暗号化(scrypt + Fernet)必須。インポート時、既存のホスト鍵記録は上書きしない
- **P2P 共有**: サーバーを介さず Hashi 同士で接続情報を直接送受信。使い捨て ECDH + 6 桁の確認コード(SAS)照合で中間者を検知。受信時だけポートを開く
- **アカウント同期(クラウド)**: Google Drive の appDataFolder に接続情報を置いて別端末と同期。端末側でマスターパスフレーズ由来の鍵で E2E 暗号化してからアップロードするため、クラウド側には暗号化済みデータしか渡らない。専用サーバー不要でクライアント完結(Google 連携ライブラリは任意依存 `requirements-cloud.txt`)

### サーバー管理(sudo 必須)
- **SSH サーバー設定の変更**: パスワード認証の無効化 / ポート番号の変更。締め出し防止のため、パスワード無効化は「鍵で実際にログインできる」ことを検証できたときだけ実行し、変更前バックアップ・`sshd -t` 構文検証・疎通確認・失敗時の自動ロールバックを行う
- **サーバーの IP を固定**(netplan / Ubuntu Server 限定): 静的 IP を適用。バックアップ + 一定時間内に疎通確認できなければ自動で元へ戻す(締め出し防止)。※実 Ubuntu での通し検証は未実施(実機確認推奨)

## 設定(ファイル → 設定)
sudo 送信ボタンの表示 / 右クリック貼り付け / 権限無視の既定 / 内蔵エディタで開く / ターミナル・エディタの文字サイズ・タブ幅。
保存先: `%APPDATA%\Hashi\`(profiles.json / known_hosts.json / settings.json / フォールバック時 creds.dat)。

## 既知の制限
- ターミナルは pyte ベース。代替画面(smcup/rmcup)・マウスレポート・ブラケットペーストに対応済みで vim/htop 内のマウス操作も効くが、xterm 完全互換ではない
- 権限無視は操作中のごく短時間だけ権限を緩める。プロセス強制終了時はその瞬間は緩んだままになるが、次回同じサーバーへ接続したときにジャーナルから自動復元する
- ポートフォワードは GUI から追加可能。ローカル (-L) / リモート (-R) / ダイナミック (-D / SOCKS5) とも実 sshd で実機検証済み
- `~/.ssh/config` は Host エイリアス(HostName / User / Port / IdentityFile / ProxyJump)に対応。ProxyCommand(外部コマンド経由)は未対応
- **サーバーの IP 固定は netplan(Ubuntu Server)限定**で、実 Ubuntu での通し適用は未検証。パスワード認証の無効化 / ポート変更も含め、サーバー設定変更系は自動ロールバックを備えるが実運用前の確認を推奨
- アカウント同期は Google Drive 実装を同梱するが、実 Google 環境での認証・往復は未検証(コア暗号・同期ロジックはテスト済み)
- sudo 送信ボタンは保存済みパスワードが前提。独自プロンプトには反応しないことがある(右クリック→送信で対応)

## 開発 / テスト

```bash
pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen pytest        # GUI はオフスクリーンでテスト
python -m compileall main.py hashi tools
```

テストはネットワーク不要(フェイク SSH を使用)。権限無視・ジャーナル・クラッシュ復元・
認証情報の暗号化往復・TOFU・パスワードプロンプト検知をカバー。

コントリビュートは大歓迎です。[CONTRIBUTING.md](CONTRIBUTING.md) を読んでから、
[Issues](https://github.com/shumaimai/SSH.FTP-/issues) の `good first issue` あたりからどうぞ。
脆弱性の報告は [SECURITY.md](SECURITY.md) へ。

## ビルド(Windows exe)

```bash
pip install pyinstaller
pyinstaller --noconfirm Hashi.spec      # dist/Hashi.exe
```

## リリース

1. `hashi/__init__.py` の `__version__` を上げ、`CHANGELOG.md` を更新
2. `vX.Y.Z` タグを push(タグは `__version__` と一致必須。CI が検証)

```bash
git tag v0.2.0 && git push origin main --tags
```

GitHub Actions が Windows で `Hashi.exe` をビルドし、Release に添付します。
詳しい開発メモ・設計判断・引き継ぎ事項は [CLAUDE.md](CLAUDE.md) を参照。

## 構成
```
main.py                エントリポイント(ダークテーマ)
hashi/config.py        プロファイル / 設定 / known_hosts の永続化
hashi/credentials.py   認証情報の保存(keyring 優先 / Fernet 暗号化フォールバック)
hashi/ssh_core.py      paramiko ラッパ(認証・TOFU・exec・sudo 実行)
hashi/terminal.py      ターミナル(IME / 全角 / 選択コピー / 右クリック貼付 / プロンプト検知)
hashi/privilege.py     権限無視スイッチのコア(一時 chmod → 操作 → 復元 / sudo フォールバック / 参照カウント)
hashi/permjournal.py   権限変更のジャーナル(緩める前に fsync 記録 → クラッシュ後に次回接続で復元)
hashi/editor.py        内蔵コードエディタ(行番号 / ハイライト / リモート保存)
hashi/forward.py       ローカルポートフォワード (-L)
hashi/filebrowser.py   SFTP ブラウザ + 2 ワーカー + 2 段階確認 + 権限無視統合
hashi/dialogs.py       接続 / ホスト鍵 / 秘密入力 / 設定 / トンネル ダイアログ
hashi/mainwindow.py    サイドバー + セッションタブ + 接続ワーカー + 自動入力配線
tools/doctor.py        CLI 接続診断
```
