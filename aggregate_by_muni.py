#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REIT物件を市区町村ごとに集約する
================================
run_reits.py が出力した reit_properties.csv を読み、東京の市区町村ごとに
  - 鑑定還元利回り(cap rate)の中央値   ← 開示物件のみで計算
  - 物件数                              ← 鑑定評価額がある物件をカウント
  - 代表事例JSON(物件名・cap・鑑定評価額) ← 駅ページ表示用
を集計し、reit_by_muni.csv を出力する。

駅ページの既存ACFフィールドに対応:
  reit_cap_median  → 周辺REIT 鑑定還元利回り中央値(%)
  reit_count       → 周辺REIT物件数
  reit_examples    → 周辺REIT事例JSON

【設計方針(ユーザー合意済み)】
- 利回りは「鑑定採用還元利回り」(CaprateMapの鑑定CRと同じ)。NOI利回りではないので
  表示ラベルは「鑑定還元利回り」とする。
- cap rate中央値は cap_rate が取れた物件のみで計算(非開示物件は除外)。
- 物件数は鑑定評価額がある物件をカウント(cap rate非開示でも物件は存在するため)。
- 東京の23区+市部のみ対象(noitasは東京の不動産DB)。
"""
import csv
import json
import re
import statistics
import argparse

# 東京23区
TOKYO23 = ['千代田区', '中央区', '港区', '新宿区', '文京区', '台東区', '墨田区', '江東区',
           '品川区', '目黒区', '大田区', '世田谷区', '渋谷区', '中野区', '杉並区', '豊島区',
           '北区', '荒川区', '板橋区', '練馬区', '足立区', '葛飾区', '江戸川区']


def extract_tokyo_muni(location, region):
    """所在地(優先)またはregionから東京の市区町村名を抽出。東京外はNoneを返す。"""
    text = (location or '')
    # 23区を直接探す(「東京都港区…」「港区…」どちらでも)
    for w in TOKYO23:
        if w in text:
            return w
    # 東京都の市部(「東京都八王子市」等)
    m = re.search(r'東京都([^\s]+?市)', text)
    if m:
        return m.group(1)
    # locationが空/東京外表記でも、regionが東京を示すなら23区総体として扱えないので None
    # (region は「東京23区」等の粗い区分でしかなく、個別区に落とせないため集約対象外)
    return None


def to_float(s):
    try:
        return float(s) if s not in (None, '') else None
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='infile', default='reit_properties.csv')
    ap.add_argument('--out', default='reit_by_muni.csv')
    ap.add_argument('--examples', type=int, default=5, help='事例JSONに載せる物件数')
    args = ap.parse_args()

    with open(args.infile, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))

    # 市区町村ごとに物件を集める
    by_muni = {}
    for r in rows:
        muni = extract_tokyo_muni(r.get('location'), r.get('region'))
        if not muni:
            continue
        by_muni.setdefault(muni, []).append(r)

    out_rows = []
    for muni, props in sorted(by_muni.items()):
        # cap rate中央値(開示物件のみ)
        caps = [to_float(p.get('cap_rate')) for p in props]
        caps = [c for c in caps if c is not None]
        cap_median = round(statistics.median(caps), 2) if caps else ''

        # 物件数(鑑定評価額がある物件)
        with_appraisal = [p for p in props if to_float(p.get('appraisal_value')) is not None]
        count = len(with_appraisal)

        # 代表事例(鑑定評価額の大きい順に、cap rateが取れているものを優先)
        def sort_key(p):
            has_cap = 1 if to_float(p.get('cap_rate')) is not None else 0
            av = to_float(p.get('appraisal_value')) or 0
            return (has_cap, av)
        examples = []
        for p in sorted(with_appraisal, key=sort_key, reverse=True)[:args.examples]:
            examples.append({
                'name': p.get('property_name', ''),
                'reit': p.get('reit_name', ''),
                'cap': to_float(p.get('cap_rate')),
                'appraisal': to_float(p.get('appraisal_value')),
                'occupancy': to_float(p.get('occupancy')),
                'use': p.get('use_type', '') or p.get('region', ''),
            })

        out_rows.append({
            'muni': muni,
            'reit_cap_median': cap_median,
            'reit_count': count,
            'reit_cap_disclosed': len(caps),   # cap rateが取れた物件数(参考)
            'reit_examples': json.dumps(examples, ensure_ascii=False),
        })

    with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['muni', 'reit_cap_median', 'reit_count',
                                          'reit_cap_disclosed', 'reit_examples'])
        w.writeheader()
        w.writerows(out_rows)

    # サマリ
    print(f"集約完了: {len(out_rows)}市区町村 -> {args.out}")
    print(f"{'市区町村':<10}{'物件数':>6}{'cap開示':>7}{'還元利回り中央値':>16}")
    for r in out_rows:
        cm = f"{r['reit_cap_median']}%" if r['reit_cap_median'] != '' else '—'
        print(f"{r['muni']:<10}{r['reit_count']:>6}{r['reit_cap_disclosed']:>7}{cm:>16}")


if __name__ == '__main__':
    main()
