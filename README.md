# dxcc-pilot — DXCC代行ワッチ（DXCC水先案内人）

FT8のデコード結果を常時監視し、**バンドごとに未交信のDXCCエンティティ**が受信できたときだけ、スマートフォンにメールで知らせるスクリプトです。CQ ham radio 2027年1月号の記事で紹介したシステムの公開版です。

> An unattended watch for FT8: it e-mails your phone only when a DXCC entity you have not yet worked on that band is decoded. Japanese documentation only; built around Turbo HAMLOG.

**構築の手順は、こちらのページで6画面に分けて説明しています。**
https://ja1bbe.github.io/dxcc-pilot/

## 何をするものか

```
SDR または無線機 → WSJT-X（FT8のデコード）
                     │ UDPマルチキャスト
                     ├→ JTAlert（画面表示・交信記録）
                     └→ dxcc_notify.py（判定・通知）→ Gmail → スマートフォン
```

- 交信済みかどうかは、Turbo HAMLOG のデータファイル（Hamlog.hdb）を直接読んで判定します（読み取り専用。HAMLOGのファイルには書き込みません）。ADIF形式のログにも対応しています。
- 通知メールはHTML版で、受信時の宇宙天気（Kp指数・太陽風など）のグラフ、電離層の観測値（国分寺）、そのエンティティとの交信記録を添えます。
- 運用の開始時と終了時にもメールを送ります。開始のメールにはバンド別のDXCC実績と、3か月以内のDXペディション予定を載せます。
- LoTWのアカウントがあれば、確認済み（QSL）を1日1回取得し、交信数と確認数を分けて表示します。

これは**聞き逃しを防ぐための補助**です。通知を受けて応答するかどうかは、必ず運用者自身が判断してください。無人で自動的に交信するものではありません。

## 動作環境

- Windows 10 / 11
- Python 3.10 以上（筆者は 3.13）と、追加のパッケージ `matplotlib`、`truststore`
- WSJT-X、JTAlert
- Turbo HAMLOG（または ADIF 形式の交信ログ）
- 通知の送信に使う Gmail アカウント（2段階認証とアプリパスワードが必要）

## ファイルの構成

| ファイル | 役割 |
|---|---|
| `dxcc_notify.py` | 本体。WSJT-Xのデコードを受け取り、判定して通知する |
| `notify_html.py` | 通知メールのHTML版とグラフを作る |
| `hamlog_reader.py` | HAMLOGのデータファイルを読む（単独で実行すると自己点検） |
| `hamlog_to_lotw.py` | HAMLOGの交信から、LoTWに提出するADIFを作る（必要な方のみ） |
| `dxcc_settings.py` | `settings.ini` を読む |
| `settings.ini` | **自局の設定**。コールサインやメールアドレスはここだけに書きます |
| `start_dxcc.bat` | 起動用ファイルの見本 |
| `common\` | Big CTY の `cty_latest.dat` と `cty_latest.csv` を置くフォルダ |
| `logs\` | ADIF形式のログを使う場合に置くフォルダ |

## 入手のしかた

画面右上の緑色の「Code」ボタンから「Download ZIP」を選び、展開したフォルダを使います。版を指定して入手したい場合は、右側の「Releases」から選んでください。

起動すると、最初に `dxcc-pilot 版 1.0.0` のように版が表示されます。お問い合わせの際は、この版をお知らせください。

## データの出典

このシステムは、次のデータを利用しています。各提供元に感謝します。

- **宇宙天気**：NOAA Space Weather Prediction Center（https://www.swpc.noaa.gov/）。各PCから直接取得します。
- **電離層の観測値**：情報通信研究機構（NICT） 電離圏観測データ（https://wdc.nict.go.jp/Ionosphere/index.html）。国分寺の観測値を、筆者のリポジトリが1時間に1回取得して公開している `iono.json` を読みます。各PCからNICTへは直接アクセスしません。
- **DXペディション予定**：NG3K Announced DX Operations（Bill Feidt, NG3K、https://www.ng3k.com/Misc/adxo.html）。筆者のリポジトリが1日2回取得して公開している `dxpeditions.json` を読みます。各PCからNG3Kへは直接アクセスしません。
- **国別データ（Big CTY）**：AD1C Jim Reisert 氏（https://www.country-files.com/）。各自でダウンロードして `common\` に置きます。

NICT と NG3K のデータは、提供元の了承を得て筆者のサイトに掲載しているものを、筆者のリポジトリが1か所だけで取得し、それを各PCで共有する形にしています。このスクリプトを改造して、NICT や NG3K に直接アクセスする形にはしないでください。

## ご注意

- **HAMLOGの読み込みは、公式の仕様によるものではありません。** 筆者の局のデータで格納のしかたを確かめて作ったものです。最初に `python hamlog_reader.py Hamlog.hdbのパス` を実行し、交信の件数と期間がHAMLOGの表示と合うかを確かめてください。
- **エンティティの判定はプレフィックスによります。** 特殊なコールサインや、Big CTY にまだ反映されていないDXペディションのコールでは、誤判定があり得ます。Big CTY は定期的に最新版に更新してください。
- **「交信済み」の判定は、DXCCの実績管理そのものではありません。** Honor Roll にはコンファームが必要です。LoTW や Club Log での確認と併用してください。
- **Gmail の仕様変更**により、アプリパスワードでの送信が将来使えなくなる可能性があります。
- **アマチュア局を遠隔で操作する場合**は、電波法令と免許状の条件に従ってください。

## お問い合わせ

CQ誌の記事と、このシステムについてのご質問は、ja1bbe@jarl.com までお寄せください。よくいただくご質問は、構築手順のページの「よくある質問」にまとめます。

## ライセンス

MIT License です（`LICENSE` を参照）。自由に使用・改変・再配布できますが、無保証です。改変して配布する場合も、上の「データの出典」の条件は守ってください。

JA1BBE 出野義則
