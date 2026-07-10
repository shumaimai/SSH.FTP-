# コントリビュートガイド

Hashi への参加ありがとうございます。小さな修正から大歓迎です。
まず [README.md](README.md) で全体像を、[CLAUDE.md](CLAUDE.md) で設計判断・ハマりどころ・
ロードマップを読んでください(CLAUDE.md は AI/人間どちらの新規参加者向けにも書かれた開発ガイドです)。

## セットアップ

```bash
git clone https://github.com/shumaimai/SSH.FTP-.git
cd SSH.FTP-
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python main.py                                  # 起動(GUI)
```

Python 3.10+ が必要です。

## テストと確認(PR の前に必ず)

```bash
QT_QPA_PLATFORM=offscreen pytest        # ネットワーク不要(フェイク SSH 使用)
python -m compileall main.py hashi tools
ruff check .                            # 参考(CI では落とさない)
```

- ヘッドレス環境で GUI を触るときは必ず `QT_QPA_PLATFORM=offscreen`。
- GUI に絡む変更は、可能ならローカルで実際に起動して確認してください。

## 何から手を付けるか

- [Issues](https://github.com/shumaimai/SSH.FTP-/issues) にロードマップ由来のタスクがあります。
  `good first issue` ラベルが付いたものが入りやすいです。
- 大きめの変更(アーキテクチャに触るもの)は、着手前に Issue で方針を相談してください。

## お作法

- **UI・コメント・コミットメッセージは日本語**で統一しています。踏襲してください。
- 権限無視まわり(`hashi/privilege.py` / `hashi/permjournal.py`)を触るときは
  **必ず対応するテストを追加・更新**してください。事故るとサーバー側のファイル権限を
  壊しかねない箇所です。設計意図は CLAUDE.md の「効いた判断とハマりどころ」を先に読むこと。
- 未検証のことは未検証と正直に書く(PR 説明・コード内コメントとも)。
- 1 PR = 1 テーマ。無関係なリファクタを混ぜない。

## PR の流れ

1. フォークしてブランチを切る(例: `fix/terminal-ime-xxx`)。
2. 変更 + テスト。上記のテストコマンドを通す。
3. PR を作成。テンプレートに沿って「何を・なぜ・どう確認したか」を書く。
4. CI(GitHub Actions)が通ることを確認。

## バグ報告・機能要望

[Issue テンプレート](https://github.com/shumaimai/SSH.FTP-/issues/new/choose) からどうぞ。
再現手順・環境(OS / Python / 接続先サーバーの種類)があると助かります。

## セキュリティ

脆弱性(認証情報の漏えい、権限復元の不備など)は公開 Issue ではなく
[SECURITY.md](SECURITY.md) の手順で報告してください。
