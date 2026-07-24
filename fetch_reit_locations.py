#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REIT公式サイトから物件所在地を取得する(恒久データ生成)
====================================================
有価証券報告書の物件表に住所を載せない法人がある。実データで確認した例:

    アドバンス・レジデンス   288件  region「東京23区」のみ
    大和証券リビング         238件  region「三大都市圏」のみ
    コンフォリア             175件  region「準都心」のみ
    野村不動産マスターファンド 285件  region「東京圏」のみ
    ＫＤＸ                   342件  region「東京経済圏」のみ

これらは有報に住所が「書かれていない」ので、パーサーをどう直しても取れない。
一方、各法人の公式サイトのポートフォリオ一覧には物件ごとの所在地がある。

【設計方針】
- 実行は「一度だけ」。出力 reit_locations.csv は恒久データとしてリポジトリに置く。
  四半期更新のたびに全件を取り直さない(--only で新規法人だけ追加する)。
- 表の読み取りは reit_parser の table_to_matrix / map_columns を再利用する。
  ヘッダ名から列を特定する方式なので、法人ごとの書式差に強い(同じ理由で有報が読めている)。
- 取れなかった法人は握りつぶさず、レポートに残す。

使い方:
    python3 fetch_reit_locations.py                # 定義済みの法人を全部
    python3 fetch_reit_locations.py --only アドバンス
    python3 fetch_reit_locations.py --dry-run      # 取得だけして件数を見る
"""
import argparse
import csv
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

from reit_parser import table_to_matrix, map_columns, combine_headers, clean_name

# 法人名 -> ポートフォリオ一覧のURL。
# 「1ページに全物件が載っていて、所在地の列がある」ページを指定すること。
# アドバンス・レジデンスは実地で確認済み(288件が1ページ、所在地は「東京都品川区」形式)。
# 他法人はURLを確認しながら順次埋める。空にしておけば単にスキップされる。
PORTFOLIO_URLS = {
    # ---- 実際にページを開いて表構造を確認済み ----
    # 288件が1ページ。所在地は「東京都品川区」形式(区まで)。
    'アドバンス・レジデンス投資法人': 'https://www.adr-reit.com/portfolio/list/',
    # 175件が1ページ。所在地は「東京都中央区日本橋人形町」形式(町名まで)。ARIより細かい。
    'コンフォリア・レジデンシャル投資法人': 'https://www.comforia-reit.co.jp/ja/portfolio/index.html',

    # ---- 一覧ページのURLは特定したが、表構造は未確認 ----
    # まず --dry-run で件数を見ること。0件ならJS描画かURL違い。
    'ＫＤＸ不動産投資法人': 'https://www.kdx-reit.com/ja/portfolio/list.html',
    'ユナイテッド・アーバン投資法人': 'https://www.united-reit.co.jp/ja/portfolio/index.html',
    # 野村MFは物件詳細ページのtitleが「{{ name }}」のまま出ており、
    # JavaScript描画の可能性が高い。requestsでは取れないかもしれない。
    '野村不動産マスターファンド投資法人': 'https://www.nre-mf.co.jp/ja/portfolio/index.html',
    '大和証券リビング投資法人': 'https://www.daiwa-securities-living.co.jp/ja/portfolio/list.html',

    # 検索結果に表の中身が出ており、サーバー描画を確認済み
    # (「ラウンドクロス一番町 / 東京都心6区 / 東京都千代田区 / 1994年3月 ...」)
    'オリックス不動産投資法人': 'https://www.orixjreit.com/ja/portfolio/list.html',

    # ---- 住宅物件がほぼ無く、駅ページには効かない法人 ----
    # 第1弾マップ(全用途)では所在地が要るので、必要になったら有効化する。
    # 'ユナイテッド・アーバン投資法人': 'https://www.united-reit.co.jp/ja/portfolio/index.html',
    # '日本都市ファンド投資法人': 'https://www.jmf-reit.com/portfolio/list.html',
    # '日本ビルファンド投資法人': 'https://www.nbf-m.com/nbf/portfolio/list.html',

    # ---- 未調査(住宅は各29件/14件と少ない) ----
    'ＮＴＴ都市開発リート投資法人': '',
    '日本ホテル＆レジデンシャル投資法人': '',
}


UA = {'User-Agent': 'Mozilla/5.0 (compatible; noitas-reit-locations/1.0)'}

# 所在地として採用できる文字列か。都道府県で始まるものだけを通す。
PREF_RX = re.compile(
    r'^(北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|埼玉県|千葉県|'
    r'東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|岐阜県|静岡県|愛知県|三重県|'
    r'滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|鳥取県|島根県|岡山県|広島県|山口県|徳島県|'
    r'香川県|愛媛県|高知県|福岡県|佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県)')


def extract_from_html(html):
    """ページ内の全テーブルから (物件名, 所在地) を集める。
    reit_parser と同じ「ヘッダ名で列を特定する」方式を使う。"""
    soup = BeautifulSoup(html, 'lxml')
    out = {}
    for table in soup.find_all('table'):
        matrix = table_to_matrix(table)
        if len(matrix) < 3:
            continue
        for hi in (0, 1):
            if hi >= len(matrix) - 1:
                continue
            for depth in (1, 2):
                if hi + depth >= len(matrix):
                    continue
                header = combine_headers(matrix, hi, depth)
                mapping = map_columns(header)
                if 'property_name' not in mapping or 'location' not in mapping:
                    continue
                ni, li = mapping['property_name'], mapping['location']
                for row in matrix[hi + depth:]:
                    if len(row) <= max(ni, li):
                        continue
                    name = clean_name(row[ni])
                    loc = (row[li] or '').strip()
                    if not name or not PREF_RX.match(loc):
                        continue
                    out.setdefault(name, loc)
                if out:
                    break
            if out:
                break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='reit_locations.csv')
    ap.add_argument('--only', default='', help='法人名の部分一致で絞る')
    ap.add_argument('--sleep', type=float, default=2.0)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    targets = {k: v for k, v in PORTFOLIO_URLS.items() if v}
    if args.only:
        targets = {k: v for k, v in targets.items() if args.only in k}
    if not targets:
        sys.exit('取得対象がありません。PORTFOLIO_URLS にURLを設定してください。')

    # 既存の恒久データを読み、上書きではなく追記する
    existing = {}
    try:
        with open(args.out, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                existing[(r['reit_name'], r['property_name'])] = r['location']
    except FileNotFoundError:
        pass
    print(f"既存 {len(existing)} 件")

    rows = dict(existing)
    for name, url in targets.items():
        print(f"{name} ... ", end='', flush=True)
        try:
            r = requests.get(url, headers=UA, timeout=60)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or r.encoding
            got = extract_from_html(r.text)
        except Exception as e:
            print(f"ERROR: {e}")
            time.sleep(args.sleep)
            continue
        new = 0
        for pn, loc in got.items():
            key = (name, pn)
            if key not in rows:
                new += 1
            rows[key] = loc
        print(f"{len(got)}件 (新規{new})")
        if not got:
            print("   [警告] 所在地列を持つ表が見つかりませんでした。"
                  "URLが一覧ページか、所在地がJSで描画されていないか確認してください。")
        time.sleep(args.sleep)

    if args.dry_run:
        print("\n--dry-run のため書き出しません")
        return

    with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['reit_name', 'property_name', 'location'])
        for (rn, pn), loc in sorted(rows.items()):
            w.writerow([rn, pn, loc])
    print(f"\n{len(rows)}件 -> {args.out}")


if __name__ == '__main__':
    main()
