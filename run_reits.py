#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EDINET REIT 物件データ 一括取得
==============================
edinet_reit_matches.csv (scanの結果) を読み、各REITの最新有価証券報告書から
物件データ(鑑定評価額・cap rate・所在地・稼働率等)を抽出してCSVに出力する。

使い方:
  export EDINET_API_KEY=xxxx
  # まず数社でテスト(用途がバラけるように選ぶ)
  python3 run_reits.py --limit 6 --test
  # 全法人
  python3 run_reits.py

出力:
  reit_properties.csv   物件1行のフラットなCSV(全法人ぶん)
  reit_parse_report.csv 法人ごとの成否・物件数・項目充足率(どこで失敗したか分かる)

注意:
  - 有報(formCode=07B000)を持たない法人は、過去に遡って有報を探す必要がある。
    matches.csv の「最新docID」が有報でない場合はスキップされるので、その場合は
    --days を増やして scan をやり直すか、後述の docid 手動指定で対応する。
"""
import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reit_parser import parse_property_tables, fetch_honbun_htmls, merge_property_maps

FIELDS = ['reit_name', 'edinet_code', 'doc_id', 'period', 'property_name',
          # 用途: use_type は正規化後(7分類)、use_type_raw は有報の原文。
          # asset_type は「資産の種類」(不動産信託受益権 等)で用途ではない。
          # 原文を残すのは、正規化が正しいか後から検証できるようにするため。
          'use_type', 'use_type_raw', 'asset_type',
          # 取得年月日: 保有期間・年率換算(CAGR)の算出に必要。
          # precision は 'day'(日まで判明) / 'month'(月初で補完) の別。
          'acquisition_date', 'acquisition_date_raw', 'acquisition_date_precision',
          'acquisition_price', 'book_value', 'appraisal_value', 'cap_rate', 'appraiser',
          'location', 'region', 'land_area', 'gross_floor_area', 'leasable_area', 'leased_area',
          'occupancy', 'tenant_count', 'investment_ratio', 'rental_income',
          'discount_rate', 'terminal_cap']

# レポートで充足率を見る項目。use_type と acquisition_date を追加しないと
# 2026-07 のパーサー修正が効いたかどうかをレポートで確認できない。
CHECK_FIELDS = ['acquisition_price', 'appraisal_value', 'cap_rate', 'appraiser',
                'location', 'occupancy', 'use_type', 'acquisition_date']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--matches', default='edinet_reit_matches.csv')
    ap.add_argument('--limit', type=int, default=0, help='先頭N法人だけ処理(0=全部)')
    ap.add_argument('--test', action='store_true', help='用途がバラけるよう代表法人を選んで実行')
    ap.add_argument('--sleep', type=float, default=1.0)
    ap.add_argument('--out', default='reit_properties.csv')
    ap.add_argument('--report', default='reit_parse_report.csv', help='法人別レポートの出力先')
    args = ap.parse_args()

    api = os.environ.get('EDINET_API_KEY')
    if not api:
        sys.exit('環境変数 EDINET_API_KEY を設定してください')

    with open(args.matches, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))

    # 有報(07B000)を最新書類に持つ法人のみ対象
    targets = [r for r in rows if str(r.get('最新formCode', '')).zfill(6) == '07B000']

    if args.test:
        # 用途がバラけるように代表を選ぶ(オフィス/住宅/物流/商業/総合/ホテル)
        want = ['日本ビルファンド', 'アドバンス・レジデンス', '日本プロロジスリート',
                'イオンリート', 'オリックス不動産', 'いちごホテルリート']
        picked = []
        for w in want:
            for r in targets:
                if w in r['REIT名(サイト表記)']:
                    picked.append(r)
                    break
        targets = picked or targets[:6]
    if args.limit:
        targets = targets[:args.limit]

    print(f"対象: {len(targets)}法人\n")

    all_rows = []
    report = []
    dropped_keys = set()   # FIELDS に無いためCSVに出せなかった項目
    for i, r in enumerate(targets, 1):
        name = r['REIT名(サイト表記)']
        doc_id = r['最新docID']
        code = r['EDINETコード']
        desc = r.get('最新docDescription', '')
        print(f"[{i}/{len(targets)}] {name} ({doc_id}) ...", end=' ', flush=True)
        try:
            # 有報の本文HTMLは複数ファイルに分割されていることがあるため全て読み、
            # 物件名でマージする(物件表と鑑定評価表が別ファイルの法人に対応)
            htmls = fetch_honbun_htmls(doc_id, api)
            maps, used = [], []
            for h in htmls:
                mp, us = parse_property_tables(h)
                if mp:
                    maps.append(mp)
                    used.extend(us)
            merged = merge_property_maps(maps)
        except Exception as e:
            print(f"ERROR: {e}")
            report.append({'reit_name': name, 'doc_id': doc_id, 'status': f'ERROR: {e}',
                           'properties': 0, **{f: '' for f in CHECK_FIELDS}})
            time.sleep(args.sleep)
            continue

        n = len(merged)
        cov = {}
        for f in CHECK_FIELDS:
            c = sum(1 for rec in merged.values() if f in rec)
            cov[f] = f"{100*c//n}%" if n else '0%'
        status = 'OK' if n > 0 else 'NO_PROPERTIES'
        print(f"{n}物件  取得価格 {cov.get('acquisition_price')}  鑑定評価額 {cov.get('appraisal_value')}"
              f"  用途 {cov.get('use_type')}  取得日 {cov.get('acquisition_date')}")

        for rec in merged.values():
            row = {f: '' for f in FIELDS}
            row.update({'reit_name': name, 'edinet_code': code, 'doc_id': doc_id, 'period': desc})
            for k, v in rec.items():
                if k in row:
                    row[k] = v
                else:
                    # パーサーが返したのに FIELDS に無い項目は「黙って消える」。
                    # 実際に use_type_raw 等を追加した際にこれで事故る構造だったので、
                    # 気づけるように一度だけ警告する(出力されなければ存在しないのと同じ)。
                    dropped_keys.add(k)
            all_rows.append(row)

        report.append({'reit_name': name, 'doc_id': doc_id, 'status': status,
                       'properties': n, **cov})
        time.sleep(args.sleep)

    with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)

    with open(args.report, 'w', newline='', encoding='utf-8-sig') as f:
        cols = ['reit_name', 'doc_id', 'status', 'properties'] + CHECK_FIELDS
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(report)

    if dropped_keys:
        print(f"\n[警告] パーサーは返したが FIELDS に無いためCSVに出力されなかった項目: "
              f"{sorted(dropped_keys)}")
        print("       必要なら FIELDS に追加してください。")

    print(f"\n完了: {len(all_rows)}物件 -> {args.out}")
    print(f"      法人別レポート -> {args.report}")
    ok = sum(1 for r in report if r['status'] == 'OK')
    print(f"      成功 {ok}/{len(report)} 法人")


if __name__ == '__main__':
    main()
