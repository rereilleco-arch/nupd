# noitas データ更新リポジトリ

四半期ごとに、MLIT取引価格CSV＋FUDOSAN DBから駅データを再生成し、WordPressが取り込む公開CSVを更新する。

## 使い方(四半期に1回)
1. 不動産情報ライブラリから東京の取引価格CSVをダウンロード
2. `input/mlit/` にそのCSVを置いてコミット&プッシュ(複数期・複数ファイルOK)
3. Actions が自動実行(または「Actions」タブ→ quarterly-data-update → Run workflow で手動実行)
4. `output/station_prices.csv` と `output/fudosan_enrichment.csv` が更新される
5. WordPress側が四半期wp-cronで自動取得(手動取得ボタンもあり)

## 初期設定
- Settings → Secrets and variables → Actions → New repository secret
  - `FUDOSANDB_API_KEY` = FUDOSAN DB のAPIキー
- リポジトリは public(WPがrawで取得するため)

## WordPressに設定するURL(rawの固定URL)
- `https://raw.githubusercontent.com/<ユーザー名>/<リポジトリ名>/main/output/station_prices.csv`
- `https://raw.githubusercontent.com/<ユーザー名>/<リポジトリ名>/main/output/fudosan_enrichment.csv`
