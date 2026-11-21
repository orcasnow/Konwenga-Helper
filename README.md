# Konwenga Helper / Dankoba Helper

Jリーグの試合記事づくりを支援する2つのアプリを収録したプロジェクトです。試合後のデータ分析には **Konwenga Helper**、試合前後の記事の下書き作成には **Dankoba Helper** を使います。どちらもGoogle Colabから利用できます。

## アプリを選ぶ

| アプリ | こんなときに使う | 入力 | 主な出力 | Colabノートブック |
| --- | --- | --- | --- | --- |
| **Konwenga Helper 9.4.2** | 試合後のスタッツを整理し、記事用の表やグラフを作りたい | Football LABの試合レポートURL | CBP・スタッツ表、レーダーチャート、ボール保持タイムライン、シュートチャートをまとめたGoogleドキュメント | [`Konwenga_Helper.ipynb`](Konwenga_Helper.ipynb) |
| **Dankoba Helper 1.19.1** | 試合情報や記事メモをまとめ、マッチプレビューの下書きを作りたい | 大会、対戦カード、記事番号、布陣、記事メモなど | Jリーグ公式情報などで補完したマッチプレビューのGoogleドキュメント | [`Dankoba_Helper.ipynb`](Dankoba_Helper.ipynb) |

### Dankoba HelperのWindowsローカル版

Googleアカウントを使わず、Windows上でマッチプレビューをMarkdownファイルとして作成するGUI版もあります。試合情報の入力・確認、公式サイトからの情報取得、画像添付に対応しています。Python 3.15での起動方法や単体実行ファイルの案内は[ローカル版ガイド](README_LOCAL.md)と[EXE利用ガイド](README_EXE.md)を参照してください。

## Colabで使う

1. 上の表から使うノートブックを開きます。GitHubから開く場合は、[Google Colab](https://colab.research.google.com/)の「GitHub」タブでこのリポジトリを選びます。
2. ノートブックのセルを上から順に実行し、設定欄に試合情報を入力します。
3. Googleアカウントの認証を求められたら、Google DriveとGoogle Docsへのアクセスを許可します。
4. 実行結果や警告を確認し、生成されたドキュメントを記事の下書きに利用します。

Google Docsへの出力を使う場合は、Googleアカウントが必要です。Colabでは初回実行時に依存パッケージとブラウザーをセットアップするため、完了まで時間がかかることがあります。

## Konwenga Helper

Football LABの試合レポートを読み取り、試合レビューに使うデータをGoogleドキュメントにまとめます。主な出力は次のとおりです。

- チャンスビルディングポイント（CBP）表とCBP詳細表
- 試合スタッツ表
- 今節および次節のCBPレーダーチャートとデータ表
- 時間帯別のボール保持率・シュート・ゴールのタイムライン
- 両チームのシュートチャート

ノートブックの設定欄では、試合URL、自チーム、次節の相手、チームカラーなどを指定します。実行結果サマリで成果物ごとの成功・失敗・スキップと警告を確認できます。

## Dankoba Helper

マッチプレビューの記事番号、対戦カード、布陣、記事メモなどをまとめ、記事の下書きを作成します。大会種別やチームを指定すると、Jリーグ公式サイトから日程・試合情報を探し、会場やキックオフなどの未入力項目を補います。設定に応じて天気、クラブ公式リンク、順位表、相手チームの前節ハイライトも取得します。

取得した内容を確認してから出力するため、自動取得できない情報は手入力で補えます。Colab版はGoogleドキュメントに出力します。Windowsローカル版はGoogle Docs APIを使わず、MarkdownファイルをPCに保存します。

## 画像・プライバシー

Konwenga HelperがGoogleドキュメントへ画像を挿入する際は、画像を一時的にGoogle Driveへアップロードします。既定では貼り付け後に公開リンク権限を解除し、一定期間後に一時画像をゴミ箱へ移します。設定で公開リンクの解除を無効にすると、画像が公開状態のまま残ることがあります。

Dankoba HelperのColab版は、設定に応じてJリーグ公式サイト、tenki.jp、クラブ公式サイトなどへアクセスします。ローカル版で取得を有効にした場合は、実行中のPCから各サイトへアクセスします。どちらのアプリも公式サービスとは無関係の非公式ツールです。取得先の利用規約を確認し、アクセス制限を回避したり、取得データを無断転載したりしないでください。

## ファイル

- `Konwenga_Helper.ipynb` — Konwenga HelperのColabノートブック
- `Dankoba_Helper.ipynb` — Dankoba HelperのColabノートブック
- `Dankoba_Helper_Local.py` — Dankoba HelperのWindowsローカルGUI
- `dankoba_local_core.py` — ローカル版のデータ取得・テンプレート処理
- `requirements-local.txt` — ローカル版のPython依存パッケージ
- `README_LOCAL.md` / `README_EXE.md` — ローカル版の起動・配布ガイド

`Konwenga_Helper2.ipynb`（Ver.9.4.0）と `Konwenga_Helper_v9_0_0.ipynb`（Ver.9.0.0）は過去版です。通常は現行版の `Konwenga_Helper.ipynb`（Ver.9.4.2）を使ってください。

## ライセンスとデータ

リポジトリ内のコードは[MIT License](LICENSE)で公開しています。Football LAB、Jリーグ、各クラブなどのサイトから取得するデータと、それを含む生成物の権利は各権利者に帰属します。依存パッケージはそれぞれのライセンスに従います。
