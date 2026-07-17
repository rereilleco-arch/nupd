#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REIT法人サマリ集計(全件ページ・個別ページ用)
==========================================
reit_properties.csv(EDINET由来, 全60法人・全用途) を法人ごとに集計し、
reit_by_company.csv を出力する。

各法人について:
  物件数          鑑定評価額のある物件数
  AUM_億円        取得価格合計(百万円→億円)。EDINET由来で自動計算。
                  取得価格が取れない/異常な法人は reit_names.json の手打ち値でフォールバック。
  鑑定合計_億円    鑑定評価額合計(資産規模の裏付け)
  cap中央値       開示物件のcap rate中央値
  cap開示数       cap rateが取れた物件数
  用途           reit_names.json の分類(公式・安定なので手打ちを採用)
  URL            reit_names.json の公式サイト
  properties_json 個別ページ用: その法人の全物件(鑑定評価額の大きい順)

【設計方針(ユーザー合意済み)】
- 変わらないもの(用途・公式URL)は reit_names.json の手打ちを使う。
- 変わるもの(物件数・AUM・鑑定・cap)は EDINET から自動計算。
- AUMは取得価格合計。取れない法人だけ手打ちフォールバック(ハイブリッド)。
"""
import csv
import json
import statistics
import argparse
from collections import defaultdict

# AUMを取得価格合計で出せない(取得価格が欠損/異常な)法人。手打ち値を使う。
# 検証で取得価格合計が手打ちAUMと大きくズレた法人(0%や195%等)。
AUM_MANUAL_FALLBACK = {
    '東急リアル・エステート投資法人',      # 取得価格0
    'いちごオフィスリート投資法人',        # 取得価格0
    'ヘルスケア＆メディカル投資法人',      # 取得価格0
    'ジャパンエクセレント投資法人',        # 195%(二重計上疑い)
    '積水ハウス・リート投資法人',          # 74%(一部欠損)
    '日本プライムリアルティ投資法人',      # 79%
    'ジャパン・ホテル・リート投資法人',    # 80%
    '森ヒルズリート投資法人',              # パース失敗で物件1件のみ→AUM過小
}


def to_float(s):
    try:
        return float(s) if s not in (None, '') else None
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='infile', default='reit_properties.csv')
    ap.add_argument('--names', default='reit_names.json')
    ap.add_argument('--out', default='reit_by_company.csv')
    ap.add_argument('--props-per-company', type=int, default=0,
                    help='個別ページ用に持たせる物件数(0=全件)')
    args = ap.parse_args()

    with open(args.infile, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))

    names = json.load(open(args.names, encoding='utf-8'))
    meta = {r['name']: r for r in names.get('current', [])}

    by_reit = defaultdict(list)
    for r in rows:
        by_reit[r['reit_name']].append(r)

    out_rows = []
    for reit, props in sorted(by_reit.items()):
        with_app = [p for p in props if to_float(p.get('appraisal_value')) is not None]
        caps = [to_float(p.get('cap_rate')) for p in props]
        caps = [c for c in caps if c is not None]

        # AUM: 取得価格合計(百万→億)。フォールバック対象なら手打ち。
        acq_sum = sum(to_float(p.get('acquisition_price')) or 0 for p in props) / 100
        manual_aum = to_float(meta.get(reit, {}).get('aum'))
        if reit in AUM_MANUAL_FALLBACK and manual_aum:
            aum = manual_aum
            aum_src = 'manual'
        elif acq_sum > 0:
            aum = round(acq_sum)
            aum_src = 'edinet'
        elif manual_aum:
            aum = manual_aum
            aum_src = 'manual'
        else:
            aum = ''
            aum_src = ''

        app_sum = sum(to_float(p.get('appraisal_value')) or 0 for p in with_app) / 100

        # 個別ページ用の物件リスト(鑑定評価額の大きい順)
        def sort_key(p):
            return to_float(p.get('appraisal_value')) or 0
        ranked = sorted(with_app, key=sort_key, reverse=True)
        if args.props_per_company > 0:
            ranked = ranked[:args.props_per_company]
        plist = []
        for p in ranked:
            plist.append({
                '物件': p.get('property_name', ''),
                '取得百万': to_float(p.get('acquisition_price')),
                '鑑定百万': to_float(p.get('appraisal_value')),
                'NOI利回り': to_float(p.get('cap_rate')),
                '稼働率': to_float(p.get('occupancy')),
                '用途': p.get('use_type', ''),
                '所在': p.get('location', ''),
            })

        out_rows.append({
            'reit_name': reit,
            'use': meta.get(reit, {}).get('use', ''),         # 手打ち用途
            'url': meta.get(reit, {}).get('url', ''),         # 手打ちURL
            'aum_oku': aum,
            'aum_src': aum_src,
            'property_count': len(with_app),
            'appraisal_sum_oku': round(app_sum) if app_sum else '',
            'cap_median': round(statistics.median(caps), 2) if caps else '',
            'cap_disclosed': len(caps),
            'properties_json': json.dumps(plist, ensure_ascii=False),
        })

    with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
        cols = ['reit_name', 'use', 'url', 'aum_oku', 'aum_src', 'property_count',
                'appraisal_sum_oku', 'cap_median', 'cap_disclosed', 'properties_json']
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)

    print(f"法人サマリ生成: {len(out_rows)}法人 -> {args.out}")
    print(f"{'法人':<24}{'用途':<16}{'AUM億':>7}{'源':>7}{'物件':>5}{'cap中':>7}")
    for r in out_rows:
        cap = f"{r['cap_median']}%" if r['cap_median'] != '' else '—'
        print(f"{r['reit_name'][:22]:<24}{(r['use'] or '?')[:14]:<16}{str(r['aum_oku']):>7}{r['aum_src']:>7}{r['property_count']:>5}{cap:>7}")


if __name__ == '__main__':
    main()
