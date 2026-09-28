# Dankoba Helper Local

Colab 版から独立して動く、Windows 向けのローカル GUI です。Google アカウントや Google Docs API は使いません。試合プレビューの入力内容から Markdown ファイルを作成し、選んだ画像があれば `attachments` フォルダーへコピーして Markdown から参照します。

## 起動

Python 3.10 以降を用意し、プロジェクトフォルダーで次を実行します。

```powershell
python -m pip install -r requirements-local.txt
python Dankoba_Helper_Local.py
```

Windows では `run_local_app.bat` をダブルクリックして起動できます。画面には Python 標準の Tkinter を使い、`requirements-local.txt` のパッケージはローカル処理モジュールの読み込みと公式サイト取得に使います。Google API や外部サービスの認証は不要です。

### 表示ブラウザーによる再取得（任意）

一部サイトで JavaScript 描画が必要な場合は、Playwright を追加できます。

```powershell
python -m pip install playwright
python -m playwright install chromium
```

アプリの「静的取得が拒否された場合、Playwright の表示ブラウザーで再取得する」を有効にすると、表示ありのブラウザーで試します。どちらの取得も拒否されたサイトからは情報を取得できません。その場合も Markdown は作成され、入力用メモと取得時の警告が残ります。

## 使い方

1. 画面上部のラジオボタンで大会種別を選びます。
2. 「試合情報」に自チーム、対戦相手、日時、会場などを入力します。
3. 必要なら「記事本文」に原稿やポイントを書きます。空欄の項目は執筆メモとして Markdown に残ります。
4. 「参考リンク・画像・取得設定」で画像やリンクを追加します。公式サイトのリンク収集は初期状態で有効です。
5. 「Markdown を作成」を押し、保存先を選びます。

画像は保存先の隣にある `attachments` フォルダーへコピーされ、Markdown には相対パスが入ります。Markdown と `attachments` フォルダーを一緒に移動してください。

## データ取得について

試合の詳細は手入力を基本にしています。Jリーグ公式ページから必要な情報が十分取れないケースを考慮し、試合情報を自動取得できないと記事を作れない設計にはしていません。クラブ公式サイトからのリンク収集と、任意の順位表・前節ハイライト取得は個別に切り替えられます。

取得先が HTTP 403 などで拒否した場合、Playwright を有効にしていれば表示ブラウザーで再試行します。ローカル実行は Colab のネットワークを使いませんが、接続先の IP 制限やアクセス制御が解除される保証はありません。取得できなかった箇所は手入力または参考リンクで補えます。

## ファイル

- `Dankoba_Helper_Local.py`: Tkinter GUI
- `dankoba_local_core.py`: Colab や Google Docs API に依存しないローカル用データ・テンプレート処理
- `requirements-local.txt`: ローカル実行の依存パッケージ
- `run_local_app.bat`: Windows 用起動ファイル
