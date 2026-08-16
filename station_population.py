#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""駅ごとの徒歩圏人口を算出する。

なぜ作り直すか
  従来の「駅周辺の推定人口」は 区の人口 ÷ 区内の駅数 の按分値だった。
  港区の駅はすべて同じ 7,956 になり、汐留（埋立のオフィス街）と白金台（住宅地）が
  同じ数字になる。駅の情報を含んでいない。

何を測るか
  人口の絶対値の精度ではなく「駅の性格」を出すことを目的にする。
  乗降客数（駅ごとの実測）と並べると、昼だけ人がいる街か、住んでいる街かが分かる。
    汐留   乗降225,734 / 居住 少  → オフィス街
    白金台 乗降  少     / 居住 多  → 住宅地

手法
  町丁の代表点が駅から半径 R km 以内なら、その町丁の人口を全部加算する。
  面積按分はしない（境界ポリゴンが要る）。町丁の一部だけが圏内でも全部数えるため
  過大に出るが、駅間の相対比較には足りる。都心は町丁が小さいので誤差が小さく、
  郊外は粗くなる。郊外はそもそもオフィス街との対比が問題にならない。

入力
  住民基本台帳による東京都の世帯と人口(町丁別・年齢別) 第5表  … 東京都総務局・CC BY
    https://www.toukei.metro.tokyo.lg.jp/juukiy/2026/jy26qv0500.csv
  station_coords.csv         … 駅座標
  _pipeline/choume.json      … 町丁目の代表点座標（Geolonia）

出力  station_population.csv
  station, pop_walk, hh_walk, town_n, pop_per_ride
"""
import csv, json, math, os, argparse, unicodedata, re

HERE = os.path.dirname(os.path.abspath(__file__))
# 環境変数が無くてもスクリプトの置き場所から判定する（_pipeline配下なら親、直下ならそこ）
BASE = os.environ.get('NOITAS_DIR') or (os.path.dirname(HERE) if os.path.basename(HERE)=='_pipeline' else HERE)
SRC = "https://www.toukei.metro.tokyo.lg.jp/juukiy/2026/jy26qv0500.csv"

KAN = '〇一二三四五六七八九'

def kan2num(t):
    if t.isdigit():
        return int(t)
    if '十' in t:
        a, _, b = t.partition('十')
        return (KAN.index(a) if a else 1) * 10 + (KAN.index(b) if b else 0)
    return KAN.index(t) if t in KAN else 0

def norm(s):
    """町丁名を『◯◯3丁目』形式に正規化して突合キーにする"""
    s = unicodedata.normalize('NFKC', s or '').replace(' ', '').replace('　', '')
    s = re.sub(r'([0-9]+|[〇一二三四五六七八九十]+)丁目',
               lambda m: f'{kan2num(m.group(1))}丁目', s)
    return s

def num(v):
    """人口・世帯数。該当なしは全角/半角のハイフン各種で入ってくるので数字だけ拾う"""
    v = re.sub(r'[^0-9]', '', unicodedata.normalize('NFKC', (v or '')))
    return int(v) if v else 0

def hav(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(h))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pop', default=os.path.join(HERE, 'pop_town.csv'),
                    help='未取得なら SRC から自動ダウンロードする')
    ap.add_argument('--coords', default=os.path.join(BASE, '..', 'NOITAS基本データ', 'csv', 'station_coords.csv'))
    ap.add_argument('--radius', type=float, default=1.2, help='徒歩15分=1.2km')
    ap.add_argument('--out', default=os.path.join(BASE, 'station_population.csv'))
    a = ap.parse_args()

    # 元データが無ければ取りに行く（東京都総務局・CC BY・年次更新）
    if not os.path.exists(a.pop):
        import urllib.request
        req = urllib.request.Request(SRC, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=60) as r, open(a.pop, 'wb') as f:
            f.write(r.read())
        print(f'元データを取得: {SRC}')

    # 町丁の人口。地域階層 1 が町丁の行（0 は区市町村の総数なので除く）
    towns = {}
    with open(a.pop, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if (r.get('町丁別地域階層') or '').strip() != '1':
                continue
            key = norm((r.get('地域') or '') + (r.get('町丁別地域') or ''))
            towns[key] = (num(r.get('人口／総数(人)')), num(r.get('世帯数(世帯)')))

    # 町丁の代表点
    pts = []
    with open(os.path.join(HERE, 'choume.json'), encoding='utf-8') as f:
        for k, (lat, lng, city, town) in json.load(f).items():
            if lat is None or lng is None:
                continue
            key = norm(city + town)
            if key in towns:
                p, h = towns[key]
                pts.append((float(lat), float(lng), p, h))

    with open(a.coords, encoding='utf-8-sig') as f:
        st = [(r['station'], float(r['lat']), float(r['lon']))
              for r in csv.DictReader(f) if r.get('lat') and r.get('lon')]

    out = []
    for name, lat, lon in st:
        p = h = n = 0
        for tlat, tlng, tp, th in pts:
            if abs(tlat - lat) > 0.02 or abs(tlng - lon) > 0.025:   # 粗い枝刈り
                continue
            if hav((lat, lon), (tlat, tlng)) <= a.radius:
                p += tp; h += th; n += 1
        out.append({'station': name, 'pop_walk': p, 'hh_walk': h, 'town_n': n})

    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['station', 'pop_walk', 'hh_walk', 'town_n'])
        w.writeheader(); w.writerows(out)

    hit = [r for r in out if r['town_n'] > 0]
    print(f'町丁マスタ {len(towns):,} / 座標が付いた町丁 {len(pts):,}')
    print(f'{len(hit)}/{len(out)} 駅で算出 → {a.out}')
    s = sorted(hit, key=lambda r: -r['pop_walk'])
    print('  人口が多い駅:', [(r['station'], f"{r['pop_walk']:,}") for r in s[:5]])
    print('  人口が少ない駅:', [(r['station'], f"{r['pop_walk']:,}") for r in s[-5:]])

if __name__ == '__main__':
    main()
