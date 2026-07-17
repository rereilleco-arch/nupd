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
from reit_parser import parse_property_tables, fetch_honbun_html

FIELDS = ['reit_name', 'edinet_code', 'doc_id', 'period', 'property_name', 'use_type',
          'acquisition_price', 'book_value', 'appraisal_value', 'cap_rate', 'appraiser',
          'location', 'region', 'land_area', 'gross_floor_area', 'leasable_area', 'leased_area',
          'occupancy', 'tenant_count', 'investment_ratio', 'rental_income',
          'discount_rate', 'terminal_cap']

CHECK_FIELDS = ['acquisition_price', 'appraisal_value', 'cap_rate', 'appraiser',
                'location', 'occupancy']


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
    for i, r in enumerate(targets, 1):
        name = r['REIT名(サイト表記)']
        doc_id = r['最新docID']
        code = r['EDINETコード']
        desc = r.get('最新docDescription', '')
        print(f"[{i}/{len(targets)}] {name} ({doc_id}) ...", end=' ', flush=True)
        try:
            html = fetch_honbun_html(doc_id, api)
            merged, used = parse_property_tables(html)
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
        print(f"{n}物件  cap_rate {cov.get('cap_rate')}  鑑定評価額 {cov.get('appraisal_value')}")

        for rec in merged.values():
            row = {f: '' for f in FIELDS}
            row.update({'reit_name': name, 'edinet_code': code, 'doc_id': doc_id, 'period': desc})
            for k, v in rec.items():
                if k in row:
                    row[k] = v
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

    print(f"\n完了: {len(all_rows)}物件 -> {args.out}")
    print(f"      法人別レポート -> {args.report}")
    ok = sum(1 for r in report if r['status'] == 'OK')
    print(f"      成功 {ok}/{len(report)} 法人")


if __name__ == '__main__':
    main()
