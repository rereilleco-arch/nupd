#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
駅ごとの周辺REIT事例を生成する(近接並び替え)
==========================================
reit_properties.csv(住宅フィルタ後) と 駅マスタ(tokyost) から、
各駅について「同区の住宅REIT物件を、駅の町名に近い順に並べた上位N件」を
事前計算し、reit_by_station.csv を出力する。

並び順(案B+C):
  1) 駅の町名と物件の町名が一致 → 最優先(近接)
  2) 町名部分一致 → 次
  3) 同区の残り → 駅名をシードに決定的シャッフル(駅ごとに違う順、再現性あり)
これにより「駅ごとに表示が変わる(SEO重複回避)」かつ
「意味のある範囲では近い物件が上に来る」を両立する。

出力列:
  station              駅名(post_title。CSV取込のキー)
  reit_cap_median      同区の住宅REIT cap rate中央値(駅間で同じ=区の水準)
  reit_count           同区の住宅REIT物件数
  reit_examples        近接順に並べた事例JSON(上位N件, 日本語キー)
"""
import csv
import json
import re
import hashlib
import statistics
import argparse

TOKYO23 = ['千代田区','中央区','港区','新宿区','文京区','台東区','墨田区','江東区',
           '品川区','目黒区','大田区','世田谷区','渋谷区','中野区','杉並区','豊島区',
           '北区','荒川区','板橋区','練馬区','足立区','葛飾区','江戸川区']

# 住宅判定(aggregate_by_muni.py と同一ロジック)
RESI_USE = re.compile(r'居住|住宅|レジデン|共同住宅')
NONRESI_USE = re.compile(r'オフィス|事務所|商業|物流|ホテル|宿泊|倉庫|店舗|事業所|底地|その他')
NONRESI_NAME = re.compile(r'Dプロジェクト|ロジ|物流|ロジスティ|DPL|プロロジス|GLP|オフィス|'
                          r'センタービル|モール|アウトレット|ショッピング|ホテル')
RESI_NAME = re.compile(r'レジデン|レジデンス|ハイツ|コーポ|メゾン|コンフォリア|プラウド|'
                       r'パークアクシス|アクシス|カーサ|ヴィラ|ガーデンホームズ|'
                       r'S-FORT|S-RESIDENCE|プロシード|アルティザ')
RESI_REIT = re.compile(r'レジデンシャル|アコモデーション|コンフォリア|リビング|プロシード|サムティ・レジデン')


def is_residential(r):
    use = (r.get('use_type', '') or '').strip()
    name = r.get('property_name', '') or ''
    reit = r.get('reit_name', '') or ''
    if use:
        if RESI_USE.search(use):
            return True
        if NONRESI_USE.search(use):
            return False
    if NONRESI_NAME.search(name):
        return False
    if RESI_NAME.search(name):
        return True
    if RESI_REIT.search(reit):
        return True
    return False


def extract_muni(loc):
    for w in TOKYO23:
        if w in (loc or ''):
            return w
    m = re.search(r'東京都([^\s]+?市)', loc or '')
    return m.group(1) if m else None


def extract_town(addr):
    """住所から町名(丁目・番地の手前)を抽出。先頭が漢数字の町名(五番町・九段南)にも対応。"""
    if not addr:
        return ''
    s = str(addr)
    s = re.sub(r'^.*?[区市町村]', '', s)          # 区市町村まで除去
    if not s:
        return ''
    # 「丁目」「番」「-」「数字＋丁目」の手前までを町名とする。
    # 先頭の漢数字は町名の一部として残す(五番町/六番町/四番町/九段南)。
    m = re.match(r'(.+?)(?:[一二三四五六七八九十〇\d０-９]+丁目|[０-９\d]|番|$)', s)
    town = m.group(1).strip() if m else s
    # 末尾に残った半端な漢数字境界を除かない(「九段南」等を保つ)
    return town


def norm_ke(s):
    """ケ/ヶ/が/ガ を正規化(市ケ谷=市ヶ谷)"""
    s = str(s)
    for a in ('ヶ', 'が', 'ガ', 'ケ'):
        s = s.replace(a, 'ケ')
    return s


def station_keywords(station_name, addr):
    """駅名と駅住所から、物件名照合用のキーワード集合を作る。
    例: 市ケ谷駅/千代田区五番町 -> {'市ケ谷','五番町'}
        秋葉原駅/千代田区外神田 -> {'秋葉原','外神田'}
    物件名にこれらの語が含まれれば「その駅の近く」とみなす。"""
    kws = set()
    base = re.sub(r'駅$', '', station_name)
    if len(base) >= 2:
        kws.add(norm_ke(base))
    town = extract_town(addr)
    if len(town) >= 2:
        kws.add(norm_ke(town))
    return {k for k in kws if len(k) >= 2}


def name_proximity_rank(keywords, prop_name):
    """物件名に駅キーワードが含まれれば0(近接)、なければ2。"""
    pn = norm_ke(prop_name or '')
    for k in keywords:
        if k in pn:
            return 0
    return 2


def to_float(s):
    try:
        return float(s) if s not in (None, '') else None
    except ValueError:
        return None


def stable_shuffle_key(station_name, prop_name):
    """駅名+物件名のハッシュ。駅ごとに決定的だが物件間で擬似ランダムな順序を与える。"""
    h = hashlib.md5((station_name + '|' + prop_name).encode('utf-8')).hexdigest()
    return int(h[:8], 16)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='infile', default='reit_properties.csv')
    ap.add_argument('--stations', default='input/tokyost_1.csv')
    ap.add_argument('--out', default='reit_by_station.csv')
    ap.add_argument('--limit', type=int, default=15, help='駅ごとの事例表示件数')
    args = ap.parse_args()

    # 住宅REIT物件を市区町村ごとに集める
    with open(args.infile, encoding='utf-8-sig') as f:
        props = [r for r in csv.DictReader(f) if is_residential(r)]

    by_muni = {}
    for r in props:
        muni = extract_muni(r.get('location'))
        if not muni:
            continue
        r['_town'] = extract_town(r.get('location', ''))
        r['_cap'] = to_float(r.get('cap_rate'))
        r['_app'] = to_float(r.get('appraisal_value'))
        if r['_app'] is None:
            continue   # 鑑定評価額が無い物件は事例に出さない
        by_muni.setdefault(muni, []).append(r)

    # 区ごとの中央値・件数(駅間で共通)
    muni_stat = {}
    for muni, plist in by_muni.items():
        caps = [p['_cap'] for p in plist if p['_cap'] is not None]
        muni_stat[muni] = {
            'cap_median': round(statistics.median(caps), 2) if caps else '',
            'count': len(plist),
        }

    # 駅マスタを読む(重複駅は最初の住所を採用)
    stations = {}
    with open(args.stations, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            title = (r.get('post_title') or '').strip()
            addr = (r.get('address') or '').strip()
            if title and title not in stations:
                stations[title] = addr

    out_rows = []
    for title, addr in sorted(stations.items()):
        muni = extract_muni(addr)
        if not muni or muni not in by_muni:
            continue
        st_kws = station_keywords(title, addr)
        plist = by_muni[muni]

        # 近接順に並べる: (物件名に駅キーワードを含むか, 決定的シャッフル)
        ranked = sorted(plist, key=lambda p: (
            name_proximity_rank(st_kws, p['property_name']),
            stable_shuffle_key(title, p['property_name']),
        ))
        examples = []
        for p in ranked[:args.limit]:
            examples.append({
                '物件': p.get('property_name', ''),
                'REIT': p.get('reit_name', ''),
                '取得百万': to_float(p.get('acquisition_price')),
                '鑑定百万': p['_app'],
                'NOI利回り': p['_cap'],
                '稼働率': to_float(p.get('occupancy')),
                '町名': p['_town'],
            })

        out_rows.append({
            'station': title,
            'reit_cap_median': muni_stat[muni]['cap_median'],
            'reit_count': muni_stat[muni]['count'],
            'reit_examples': json.dumps(examples, ensure_ascii=False),
        })

    with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['station', 'reit_cap_median', 'reit_count', 'reit_examples'])
        w.writeheader()
        w.writerows(out_rows)

    print(f"駅ごと近接事例を生成: {len(out_rows)}駅 -> {args.out}")
    # サンプル: 千代田区の数駅で近接が効いているか
    for r in out_rows:
        if r['station'] in ('大手町駅', '麹町駅', '市ケ谷駅'):
            ex = json.loads(r['reit_examples'])
            print(f"\n{r['station']} (中央値{r['reit_cap_median']}% / {r['reit_count']}件) 上位3:")
            for e in ex[:3]:
                print(f"  [{e['町名']:<8}] {e['物件'][:22]:<24} cap={e['NOI利回り']}")


if __name__ == '__main__':
    main()
