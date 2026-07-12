# CLAUDE.md — Hashi 開発ガイド(Claude Code 引き継ぎ用)

このファイルは Claude Code(や新しく参加する人)が**会話履歴なしで**このプロジェクトを
続けられるように書いてある。まずここを読むこと。

## これは何か

**Hashi**(橋 = ローカルとリモートをつなぐ)は、Windows で「まともに使える」SSH ターミナル +
SFTP ファイルブラウザを 1 つに統合したデスクトップアプリ。コンセプトは **PuTTY + WinSCP を
別々に開かなくていい**こと。1 接続 = 1 タブで、ターミナルと SFTP ブラウザが横並びになる。

- 技術選定: **Python 3.10+ / PySide6 / paramiko / pyte / wcwidth**(Electron は重いので不採用)。
- UI・コメント・コミットメッセージは**日本語**で統一している。踏襲すること。
- 作者は Linux サーバー運用・iOS/Flask/Discord bot 開発の経験がある高校生。直接的で
  実践的な説明を好み、技術的な制約は正直に明示してほしいタイプ。忖度した「できます」より
  「ここは未検証」とはっきり言うほうが喜ばれる。

## セットアップ / 実行 / テスト

```bash
python -m venv .venv && . .venv/bin/activate      # 任意
pip install -r requirements-dev.txt               # 実行 + 開発(pytest, pyinstaller, ruff)
python main.py                                     # 起動

QT_QPA_PLATFORM=offscreen pytest                   # テスト(GUI はオフスクリーン)
python -m compileall main.py hashi tools           # 構文チェック
```

- **ヘッドレス環境で GUI を触るときは必ず `QT_QPA_PLATFORM=offscreen`**。xcb は入っていない
  ことが多い。pytest の `qapp` フィクスチャが自動で offscreen にする。
- パッケージング: `pyinstaller --noconfirm Hashi.spec` → `dist/Hashi.exe`。

## リポジトリ構成 / モジュール責務

```
main.py                エントリポイント(main() あり)。Fusion ダークテーマを適用。
hashi/__init__.py      __version__(バージョンの単一ソース)。config が参照。
hashi/config.py        Profile / Settings / ProfileStore / KnownHosts(TOFU)の永続化。
hashi/credentials.py   認証情報保存。keyring 優先 → Fernet 暗号化ファイルにフォールバック。
hashi/ssh_core.py      paramiko Transport 直叩き。認証・TOFU・exec_command・run_sudo。GUI 非依存。
hashi/terminal.py      pyte HistoryScreen + 自前 QPainter 描画。IME / 全角 / 選択即コピー /
                       右クリック貼付 / パスワードプロンプト検知。
hashi/privilege.py     権限無視スイッチのコア。共有 PermManager(ロック+参照カウント+専用
                       SFTP チャネル)。一時 chmod → 操作 → 復元。sudo フォールバック。
hashi/permjournal.py   権限変更のジャーナル。緩める前に fsync 記録し、クラッシュ後に復元。
                       pid 生存判定で他セッションの誤爆を防ぐ。
hashi/editor.py        内蔵コードエディタ。行番号・簡易ハイライト・検索。Ctrl+S でリモート保存。
hashi/forward.py       ローカルポートフォワード(-L)。
hashi/filebrowser.py   SFTP ブラウザ。SftpWorker(nav/xfer の 2 スレッド・別チャネル)、
                       2 段階確認、権限無視統合、エディタ連携。★このファイルが一番大きい。
hashi/dialogs.py       接続 / ホスト鍵 / 秘密入力 / 設定 / トンネル ダイアログ。
hashi/mainwindow.py    LauncherWindow(接続先選択)+ SessionWindow(1 接続 1 ウィンドウ)+
                       ConnectWorker + 自動入力の配線 + SecretContext(sudo/パスワード供給源)。
                       共有メニュー操作は _SharedOps mixin。旧 MainWindow は LauncherWindow の別名。
hashi/portability.py   接続情報の書き出し/読み込み(#42)。known_hosts も含む。秘密情報は
                       パスフレーズ暗号化必須。dumps/loads_bundle は P2P と共用。
hashi/sshd_admin.py    sshd 堅牢化(#12): パスワード無効化/ポート変更。鍵ログイン検証→
                       バックアップ→sshd -t→reload→疎通確認→自動ロールバック。
hashi/p2p.py           P2P 共有(#43)。SAS 認証つき ECDH でバンドルを直接転送。
hashi/cloudsync.py     アカウント同期(#44)。バンドルを scrypt+Fernet で E2E 暗号化し
                       Google Drive appDataFolder へ put/get。backend 差し替え可能。
                       google 系ライブラリは任意依存(requirements-cloud.txt)。
tools/doctor.py        CLI 接続診断(TCP→ホスト鍵→認証→SFTP→シェル)。
tests/                 pytest(ネットワーク不要。フェイク SSH を conftest に用意)。
```

## スレッドモデル(重要)

- **GUI スレッド**: すべての QWidget。
- **ConnectWorker(QThread)**: 接続処理。秘密情報の入力は GUI に Signal で依頼し、
  `threading.Event` でブロック待機して受け取る(`provide()`)。
- **SftpWorker(QThread)× 2**: `nav`(一覧・操作)と `xfer`(転送)。それぞれ**別の SFTP
  チャネル**を持つ。ジョブキュー方式。`_dispatch` が `_job_<kind>` を呼ぶだけなので、
  新しい操作は `_job_xxx` メソッドを足して `enqueue({"kind":"xxx", ...})` すればよい。
- paramiko の 1 チャネルはスレッド安全でない。**共有 PermManager は専用チャネルを 1 本持ち、
  その利用をすべて自前の RLock で直列化する**。実転送はワーカー自身のチャネルなので並行可。

## 設計上の「効いた」判断とハマりどころ(消さない・壊さないこと)

1. **paramiko 5 の鍵ロード**: `PKey.from_path` は `password` が bytes 必須。パスフレーズ未指定でも
   cryptography が `TypeError("password must be bytes")` を投げる。`ssh_core.load_private_key` は
   常に bytes を渡し、`"unexpected keyword"` で 3.x 互換分岐、それ以外の TypeError は
   `PasswordRequiredException` に変換。パスフレーズ誤りは再入力ループ。ここは触ると壊れやすい。
2. **権限無視の書き込みビットは a+w(0o222)**。接続ユーザーは対象ファイルの所有者とは限らない
   (むしろ所有者でないから権限無視が要る)。u+w だと他人所有ファイルに効かない。**一時付与→
   即復元なので広めでも実害は最小**、という設計思想。
3. **ジャーナルの順序**: `record()`(fsync)を **chmod で緩める前**に行う。復元は「元の権限に戻す」
   だけなので**冪等**。どの段階でプロセスが死んでも安全(緩める前=まだ元のまま/緩めた後=次回戻す/
   戻した後=もう一度戻すだけ)。
4. **pid ゲート復元**: 各エントリに記録元 pid を持たせ、復元対象は「その pid がもう生きていない」
   ものだけ。これで**同じサーバーへ同時接続している生存セッションが今まさに緩めている最中の
   ファイルを別インスタンスが横から戻す事故**を防ぐ。`permjournal.pid_alive` は Windows は
   OpenProcess、POSIX は `os.kill(pid,0)`。
5. **復元にも権限が要る**: root 所有ファイルを緩めるのに sudo を使った以上、戻すのにも sudo が要る。
   起動時は保存済み sudo パスワードで自動復元し、戻せない件数(stuck)が残れば
   `recover_incomplete` シグナルでユーザーに sudo を促す。復元は**深いパスから順に**行う
   (親ディレクトリの x を先に外して子へ辿れなくなるのを防ぐ)。
6. **右クリック貼り付け**: 右クリック=貼り付け(PuTTY 流)。メニューは **Shift+右クリック**。
   左で選択したら即コピー。
7. **sudo ワンタップ送信**: プロンプト検知は保守的な正規表現(`terminal._PW_PATTERNS`)。
   リモート側はプロンプトを偽装できるため**確認なしの自動送信はしない**。sudo プロンプト
   検知時は送信ボタンを表示し、送る判断は常に人間(ワンタップ)。password/passphrase は
   別ホストの可能性があるのでボタンも出さない。誤りループ防止に **8 秒クールダウン**
   (送信直後の再プロンプトにはボタンを出さない)。手動送信は右クリックメニュー
   (→ `password_prompt.emit("manual")`)。
8. **オフスクリーン Qt**: ヘッドレスでの検証は `QT_QPA_PLATFORM=offscreen`。
9. **-R/-D の双方向ポンプ `forward._pump_stream`**: paramiko チャネルの `fileno()` は
   内部パイプの読み取り端。**select の書き込みリストに入れても「書き込み可能」には
   ならない**ので、返り経路のフラッシュは `chan.send_ready()` で判定して直接 `send` する
   (select 依存にすると応答が永久に返らない)。実ソケットを使うフェイクでは再現しないため、
   `tests/test_forward.py::test_pump_stream_return_path_via_send_ready` がパイプ端 fileno で
   この特性を固定している。

## テスト方針 / 検証済みと未検証

- **pytest(ネットワーク不要)**: `tests/`。ジャーナル・参照カウント・クラッシュ復元・
  認証情報の暗号化往復・Settings/Profile/TOFU・パスワードプロンプト検知をカバー。
  フェイク SSH は `tests/conftest.py`。
- **実 SSH 結合(手動)**: コンテナ内に sshd を立てて検証してきた。おおよそ:
  ```bash
  useradd -m tester && echo 'tester:testpass' | chpasswd && usermod -aG sudo tester
  mkdir -p /home/tester/.ssh && cp key.pub /home/tester/.ssh/authorized_keys
  mkdir -p /run/sshd && /usr/sbin/sshd -D -p 2222 -o ListenAddress=127.0.0.1 &
  # 権限無視の検証用: root 所有・mode 000 のファイル等を用意
  echo secret > /srv/secret.txt && chown root:root /srv/secret.txt && chmod 000 /srv/secret.txt
  ```
  過去の実機検証で確認済み: 鍵認証+パスフレーズ+TOFU、再帰アップロード/ダウンロード/削除、
  2 段階確認、**権限無視の読み(000→復元)・新規作成・上書き(いずれも sudo chmod + 元へ復元)**、
  日本語ファイル名の描画/選択コピー。
- **ローカルポートフォワード(-L)は実機検証済み**(2026-07-10、Issue #1)。実 sshd
  (OpenSSH 9.6)に `SshSession` で接続し、単発 GET / 5MB 転送の整合性 / 並行 8 接続 /
  到達不能リモートの異常系 / stop() 後のポート解放を通しで確認。`tests/test_forward.py` に
  フェイク Transport のユニットテスト(CI 常時)+ ライブ結合テスト(`HASHI_LIVE_SSH=1` で
  実行)として恒久化してある。

## ビルド & リリース手順

1. `hashi/__init__.py` の `__version__` を上げる。
2. `CHANGELOG.md` に追記(`[Unreleased]` → 新バージョン)。
3. コミットして **`vX.Y.Z` タグ**を push(タグは `__version__` と一致必須。CI が検証する)。
   ```bash
   git tag v0.2.0 && git push origin main --tags
   ```
4. `.github/workflows/release.yml` が windows-latest で PyInstaller ビルド → GitHub Release を
   作成し `Hashi.exe` を添付する(GITHUB_TOKEN は自動。secret 設定不要)。
5. リポジトリは `shumaimai/SSH.FTP-`。README / CHANGELOG / pyproject 内のリンクは置換済み。

- keyring は凍結時にバックエンドを取りこぼすため `Hashi.spec` で `collect_submodules("keyring")` と
  `win32ctypes` を明示収集している。Windows の資格情報マネージャ backend が動かない症状が出たら
  まずここを疑う。

## ロードマップ / 未着手(優先度つき)

- [x] **ポートフォワードの実機検証**(2026-07-10 完了。`tests/test_forward.py` 参照)。
- [x] **リモート(-R)/ ダイナミック(-D)フォワード**(#2、2026-07-10 実装 + 実機検証済み)。
  実 sshd に対し -R/-D とも単発 GET / 2MB 整合性 / 並行接続 / stop 後の解放を通し確認。
  検証中に共有ポンプ `_pump_stream` のバグを発見・修正した(下記)。
- [x] `~/.ssh/config` の読み込み(Host エイリアス。2026-07-10、`hashi/sshconfig.py`)。
- [x] **ProxyJump(多段接続)**(2026-07-11 実装 + 実機検証済み)。踏み台ごとに Transport を
  張り direct-tcpip チャネルを次ホップのソケットにする方式(`ssh_core.resolve_jump_chain` /
  `parse_jump_specs`)。踏み台の秘密入力プロンプトは「踏み台」を含める取り決めで、
  GUI 側(`ConnectWorker.get_secret`)が接続先の保存済みパスワードの流用・踏み台秘密の
  保存を抑止する。入れ子の ProxyJump(踏み台自身の ProxyJump)は平坦化を促してエラー。
  実 sshd 2〜3 台で 1 段・2 段チェーンを通し検証済み。`tests/test_proxyjump.py` に
  ユニット + ライブ結合テスト(`HASHI_LIVE_SSH=1`)として恒久化。
  **ProxyCommand は未対応のまま明示拒否**(外部コマンド実行が絡む。必要なら別 Issue)。
- [x] 転送キューの一覧 UI とレジューム(2026-07-10、Issue #5 / `hashi/transferqueue.py`)。
- [ ] 外部アプリで開いたファイルの変更監視 → 自動再アップロード(内蔵エディタは対応済み。
      「関連付けアプリで開く」経路が未対応)。
- [x] ターミナルの xterm 互換強化(Issue #6、2026-07-11 完了)。代替スクリーン(#34)・
      ブラケットペースト・マウスレポート(`?1000/?1002/?1003` + SGR `?1006`、Shift で
      ローカル選択に迂回)。実 sshd + 実 vim でクリック/ホイール/復帰を通し検証済み。
      `tests/test_terminal_mouse.py` 参照。
- [ ] terminal / editor のテスト拡充(描画・IME は手動確認中心)。
- [ ] exe への署名(アイコンは v0.3.0 で追加済み)。

## お作法

- 変更したら **`pytest` と `compileall` を通す**。GUI を絡む変更は offscreen で起動確認。
- 権限無視まわり(`privilege.py` / `permjournal.py`)は**必ず対応するテストを足す/更新する**。
  ここは事故るとサーバー側のファイル権限を壊しかねない箇所なので慎重に。
- 日本語 UI / コメントを維持。ユーザーへの説明は簡潔・率直に。未検証は未検証と書く。

## サブ機(Devin / Windsurf)への引き継ぎ運用 ★定期メンテ対象

このリポジトリはサブ機(Devin / Windsurf)にも作業させる。サブ機は **`.windsurfrules`**
を行動ルールとして読むので、CLAUDE.md に新しい設計判断・「壊してはいけない不変条件」を
足したら、その要点を `.windsurfrules` にも反映してサブ機が踏襲できるようにすること。

- **`.windsurfrules` の「役割と禁止事項」(main へ直接 commit/push/merge しない、勝手に
  タグ/リリースしない 等)は絶対に消さない・弱めないこと。** 追記はしても削除はしない。
- CLAUDE.md 側で新モジュールや不変条件(例: 今回の portability / sshd_admin / p2p /
  cloudsync、ProxyJump の秘密分離、ランチャー分割)を増やしたら、対応する注意点を
  `.windsurfrules` の「壊してはいけない重要な設計(新機能ぶん)」へ 1〜数行で足す。
- **定期的に(節目の PR ごと、または数機能ごとに)CLAUDE.md と `.windsurfrules` の差分を
  見比べ、サブ機が考慮すべき事項が漏れていないかまとめ直すこと。** 具体的には:
  「壊してはいけない設計」「スレッド/チャネル規約」「秘密情報・E2E 暗号の扱い」
  「実機検証が要る領域」の 4 観点で棚卸しし、CLAUDE.md にあって `.windsurfrules` に
  無いものを移す。逆に実装が変わってルールが古くなっていたら両方直す。
- サブ機向けの表現は簡潔な禁止・必須形(「〜しないこと」「〜を維持すること」)にする。
  背景説明は CLAUDE.md 側に厚く書き、`.windsurfrules` には結論と一行の理由だけ置く。
