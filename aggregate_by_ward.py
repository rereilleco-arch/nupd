#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
東京23区ごとのREIT投資指標を集計する(専用ページ /tokyo23-reit 用)
================================================================
reit_properties.csv と reit_locations.csv(公式サイト由来の所在地マスタ)から、
23区それぞれの「取得総額・含み益・含み益率・cap rate中央値・取得年中央値」を
事前計算し、tokyo23_wards.json を出力する。

散布図の設計:
  横軸 = 取得総額(区がどれだけ機関投資家の資金を集めたか / ストック)
  縦軸 = 含み益率 = 鑑定評価額合計/取得価格合計 - 1 (どれだけ値上がりしたか / フロー)
  色   = cap rate 中央値 (低い=投資家人気)
  円   = 含み益額

住所補完:
  有報に住所が無い法人(ARI等)は reit_locations.csv で補完する
  (aggregate_by_station.py と同じマスタ)。出所は問わず区の判定にのみ使う。

除外(第1弾マップと同じ):
  - 太陽光インフラ4法人(鑑定額が償却後で分布を壊す)
  - 取得価格を物件別に開示しないREIT(東急リアル・エステート/ヘルスケア&メディカル)
  - 23区外・所在地不明の物件

使い方:
  python3 aggregate_by_ward.py
  python3 aggregate_by_ward.py --min-count 30   # 集計に載せる区の最低物件数
"""
import argparse
import csv
import json
import re
import statistics
import unicodedata

TOKYO23 = ['千代田区', '中央区', '港区', '新宿区', '文京区', '台東区', '墨田区', '江東区',
           '品川区', '目黒区', '大田区', '世田谷区', '渋谷区', '中野区', '杉並区', '豊島区',
           '北区', '荒川区', '板橋区', '練馬区', '足立区', '葛飾区', '江戸川区']

# 他都市の同名区を東京と誤判定しないための除外(aggregate_by_station と同じ考え方)
OTHER_PREF = re.compile(
    r'(北海道|青森|岩手|宮城|秋田|山形|福島|茨城|栃木|群馬|埼玉|千葉|神奈川|新潟|富山|石川|'
    r'福井|山梨|長野|岐阜|静岡|愛知|三重|滋賀|京都|大阪|兵庫|奈良|和歌山|鳥取|島根|岡山|'
    r'広島|山口|徳島|香川|愛媛|高知|福岡|佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄)')
OTHER_CITY = re.compile(
    r'(札幌|仙台|さいたま|千葉市|横浜|川崎|相模原|新潟市|静岡市|浜松|名古屋|京都市|大阪市|'
    r'堺市|神戸|岡山市|広島市|北九州|福岡市|熊本市)')

INFRA = ['東京インフラ・エネルギー投資法人', 'カナディアン・ソーラー・インフラ投資法人',
         'エネクス・インフラ投資法人', 'ジャパン・インフラファンド投資法人']
NO_ACQ = ['東急リアル・エステート投資法人', 'ヘルスケア＆メディカル投資法人']


def ward_of(loc):
    s = str(loc or '')
    if not s:
        return None
    if '東京都' in s:
        s = s.split('東京都', 1)[1]
    elif OTHER_PREF.search(s) or OTHER_CITY.search(s):
        return None
    for w in TOKYO23:
        if w in s:
            return w
    return None


def to_f(x):
    try:
        return float(x) if x not in (None, '') else None
    except ValueError:
        return None


def load_locations(path):
    out = {}
    try:
        with open(path, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                out[(r['reit_name'], r['property_name'])] = r['location']
    except FileNotFoundError:
        pass
    return out


def median(xs):
    return statistics.median(xs) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='infile', default='reit_properties.csv')
    ap.add_argument('--locations', default='reit_locations.csv')
    ap.add_argument('--out', default='tokyo23_wards.json')
    ap.add_argument('--min-count', type=int, default=1,
                    help='集計に載せる区の最低物件数(既定1=全区、注記で件数を明示する方針)')
    args = ap.parse_args()

    locmaster = load_locations(args.locations)

    by_ward = {}
    seen = placed = 0
    for r in csv.DictReader(open(args.infile, encoding='utf-8-sig')):
        reit = r['reit_name']
        if reit in INFRA or reit in NO_ACQ:
            continue
        acq = to_f(r.get('acquisition_price'))
        apr = to_f(r.get('appraisal_value'))
        if not acq or acq <= 0 or apr is None:
            continue
        rate = apr / acq - 1
        if rate < -0.95 or rate > 4 or acq > 500000:   # 異常値ガード
            continue
        seen += 1
        loc = (r.get('location') or '').strip() or locmaster.get((reit, r.get('property_name', '')), '')
        w = ward_of(loc)
        if not w:
            continue
        placed += 1
        d = r.get('acquisition_date', '')
        by_ward.setdefault(w, []).append({
            'acq': acq, 'gain': apr - acq, 'rate': rate,
            'cap': to_f(r.get('cap_rate')),
            'ay': int(d[:4]) if d[:4].isdigit() else None,
        })

    wards = []
    for w, ps in by_ward.items():
        if len(ps) < args.min_count:
            continue
        acq = sum(p['acq'] for p in ps)
        gain = sum(p['gain'] for p in ps)
        caps = [p['cap'] for p in ps if p['cap']]
        ays = [p['ay'] for p in ps if p['ay']]
        wards.append({
            'w': w,
            'n': len(ps),
            'acq': round(acq / 1e6, 4),          # 兆円
            'gain': round(gain / 1e6, 4),        # 兆円
            'rate': round(gain / acq, 4),
            'cap': round(median(caps), 2) if caps else None,
            'cap_cov': round(len(caps) / len(ps), 2),
            'ay': round(median(ays)) if ays else None,
        })
    wards.sort(key=lambda x: -x['acq'])

    tot_acq = sum(w['acq'] for w in wards)
    tot_gain = sum(w['gain'] for w in wards)
    payload = {
        'wards': wards,
        'avg_rate': round(tot_gain / tot_acq, 4) if tot_acq else 0,
        'acq_total': round(tot_acq, 3),
        'n_total': sum(w['n'] for w in wards),
        'generated_from': args.infile,
    }
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))

    print(f"23区集計 -> {args.out}")
    print(f"  取得価格ありの物件 {seen} / 23区に配置 {placed} "
          f"({placed*100//seen if seen else 0}%)")
    print(f"  集計対象 {len(wards)}区  取得総額 {tot_acq:.1f}兆  "
          f"加重含み益率 +{payload['avg_rate']*100:.0f}%")
    low = [w['w'] for w in wards if w['n'] < 30]
    if low:
        print(f"  [注意] 30件未満の区(外れ値の影響を受けやすい): {low}")
    lowcap = [w['w'] for w in wards if w['cap_cov'] < 0.6]
    if lowcap:
        print(f"  [注意] cap rate開示が6割未満の区(色が不正確): {lowcap}")


if __name__ == '__main__':
    main()
