#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国土数値情報の地価公示(L01)から、駅ごとの公示地価を集計する。

  python3 land_price_points.py --out output/station_land_points.csv

なぜ必要か
  土地の取引は駅あたり中央値3件しかなく、land-price ページの数字が薄い。
  公示地価は東京都に2,602地点あり、取引が無い駅でも公的な評価額を出せる。
  さらに地点ごとに前年比を持つので、いま区単位49通りしかない land_yoy を
  駅単位に置き換えられる（スコアの構成要素の粒度も上がる）。

元データの利点
  L01 には最寄駅名(L01_048)と駅からの距離m(L01_050)が最初から入っている。
  座標から距離を計算する必要がなく、鑑定士が認定した最寄駅がそのまま使える。

出力（1行=1駅）
  station, pt_n, price_median, yoy_median,
  resi_n, resi_price_median, comm_n, comm_price_median,   … 住宅系/商業系
  points_json  [{addr,use,price,yoy,dist,far}] 距離順に最大8地点
"""
import argparse, csv, glob, json, os, re, statistics as st, sys, unicodedata, urllib.request, zipfile, io as _io

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get('NOITAS_DIR') or (os.path.dirname(HERE) if os.path.basename(HERE) == '_pipeline' else HERE)
# 東京都(13)。年度が変わったら L01-25 などに上げる
SRC = 'https://nlftp.mlit.go.jp/ksj/gml/data/L01/L01-24/L01-24_13_GML.zip'

# L01の属性コード
F_PRICE, F_YOY = 'L01_008', 'L01_009'      # 円/㎡、前年比%
F_ADDR = 'L01_025'                          # 所在地
F_STATION, F_DIST = 'L01_048', 'L01_050'    # 最寄駅名、駅からの距離m
F_ZONE, F_FAR = 'L01_051', 'L01_058'        # 用途地域、指定容積率%
TSUBO = 3.305785

# 用途地域の略称。住宅系か商業系かの判定に使う
COMM = ('商業', '近商', '準工', '工業', '工専')

# 鑑定評価上の駅名にはどの路線の駅かが付く（都営市ヶ谷、つくばエクスプレス浅草）。
# サイトは1駅1ページなので落として突合する。
OPERATOR = re.compile(r'^(都営|東京メトロ|つくばエクスプレス|小田急|京王|京急|京成|東急|東武|西武|相鉄|ＪＲ|JR)')


def skey(s):
    """駅名の突合キー。表記ゆれを潰す。

    実際に取りこぼした例（いずれも同じ駅）
      霞ヶ関 / 霞ケ関        … ケの大小はサイト側ですら混在している
      押上 / 押上〈スカイツリー前〉 … 副駅名の括弧。山括弧と丸括弧の両方が存在する
      市ヶ谷 / 都営市ヶ谷      … 事業者プレフィックス
    """
    s = unicodedata.normalize('NFKC', s or '').strip()
    s = re.sub(r'[〈（(\[][^〉）)\]]*[〉）)\]]', '', s)   # 副駅名
    s = re.sub(r'駅$', '', s)
    s = OPERATOR.sub('', s)
    return s.replace('ケ', 'ヶ').replace('ガ', 'ヶ').replace(' ', '')


def find_stations(path):
    """駅一覧CSVを探す。リポジトリではルート直下や input/ に置かれ、
    手元では ../NOITAS基本データ/csv/ にある。見つからないと正規化が
    黙って無効化され571駅のまま出てしまうため、候補を順に当たる。"""
    cands = [path] if path else []
    cands += [os.path.join(BASE, 'station_coords.csv'),
              os.path.join(BASE, 'input', 'station_coords.csv'),
              os.path.join(HERE, 'station_coords.csv'),
              os.path.join(BASE, '..', 'NOITAS基本データ', 'csv', 'station_coords.csv')]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def site_stations(path):
    """サイトの駅名を読む。出力はこの正式名に揃える（プラグインは駅ページの
    タイトルで引くため、L01側の名前で出すと表示されない）。
    キーが衝突する場合は両方返す。押上は山括弧版と丸括弧版が別ページで生きている。"""
    m = {}
    if not os.path.exists(path):
        return m
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            n = (r.get('station') or '').strip()
            if n:
                m.setdefault(skey(n), []).append(n)
    return m


def num(v):
    try:
        return float(str(v).replace(',', ''))
    except (TypeError, ValueError):
        return None


def fetch(dst):
    """未取得ならダウンロードして展開し、geojsonのパスを返す"""
    g = glob.glob(os.path.join(dst, '*', '*.geojson'))
    if g:
        return g[0]
    os.makedirs(dst, exist_ok=True)
    req = urllib.request.Request(SRC, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-L01-2024.html'})
    with urllib.request.urlopen(req, timeout=120) as r:
        zipfile.ZipFile(_io.BytesIO(r.read())).extractall(dst)
    print(f'元データを取得: {SRC}')
    return glob.glob(os.path.join(dst, '*', '*.geojson'))[0]


def med(v):
    return st.median(v) if v else None



def guard(out, path, minimum=1, ratio=0.8):
    """出力が痩せていたら書かずに異常終了する。

    2026-08-16、FUDOSAN DB APIが GitHub Actions から 403/404 を返し、
    0件のまま output/muni_price_trends.csv を上書きした。ヘッダだけの
    111バイトになり、それをプラグインが取得して全駅から価格推移が消えた。
    失敗が「空のCSV」という正常な形で伝播したため、誰も気づかなかった。

    データ取得の失敗は、古いデータを残したままRUNを赤くする方が安全。
    """
    if len(out) < minimum:
        sys.exit(f'中止: 取得できたのが {len(out)} 件です。'
                 f'{path} は上書きしません（取得元の障害を疑ってください）。')
    if os.path.exists(path):
        with open(path, encoding='utf-8-sig') as f:
            prev = max(0, sum(1 for _ in f) - 1)
        if prev and len(out) < prev * ratio:
            sys.exit(f'中止: 既存 {prev} 件 → 今回 {len(out)} 件と大きく減りました。'
                     f'{path} は上書きしません。意図した減少なら {path} を先に消してください。')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cache', default=os.path.join(HERE, 'l01'))
    ap.add_argument('--out', default=os.path.join(BASE, 'station_land_points.csv'))
    ap.add_argument('--max-dist', type=int, default=1500, help='駅からの距離の上限m')
    ap.add_argument('--stations', default='', help='サイトの駅一覧。出力の駅名をこれに揃える')
    a = ap.parse_args()

    sf = find_stations(a.stations)
    if not sf:
        sys.exit('中止: station_coords.csv が見つかりません。駅名を正規化できないため出力しません。')
    site = site_stations(sf)
    print(f'駅一覧: {sf}（{sum(len(v) for v in site.values())}駅）')

    ft = json.load(open(fetch(a.cache), encoding='utf-8'))['features']
    by, unmatched = {}, {}
    for x in ft:
        p = x['properties']
        stn = str(p.get(F_STATION) or '').strip()
        price = num(p.get(F_PRICE))
        dist = num(p.get(F_DIST))
        if not stn or stn == '_' or not price:
            continue
        if dist is not None and dist > a.max_dist:
            continue
        # サイトの正式名に寄せる。無ければ捨てる（都県外・島嶼部のバス停が混ざるため）
        names = site.get(skey(stn)) if site else [stn + '駅']
        if not names:
            unmatched.setdefault(stn, 0)
            unmatched[stn] += 1
            continue
        pt = {
            'addr': re.sub(r'\s+', '', str(p.get(F_ADDR) or '')),
            'use': str(p.get(F_ZONE) or '').strip(),
            'price': int(price),
            'tsubo': int(round(price * TSUBO)),
            'yoy': num(p.get(F_YOY)),
            'dist': int(dist) if dist is not None else None,
            'far': num(p.get(F_FAR)),
        }
        for nm in names:
            by.setdefault(nm, []).append(dict(pt))

    rows = []
    for stn, pts in by.items():
        pts.sort(key=lambda d: (d['dist'] if d['dist'] is not None else 99999))
        resi = [d for d in pts if not any(k in d['use'] for k in COMM)]
        comm = [d for d in pts if any(k in d['use'] for k in COMM)]
        yoy = [d['yoy'] for d in pts if d['yoy'] is not None]
        rows.append({
            'station': stn, 'pt_n': len(pts),
            'price_median': int(med([d['tsubo'] for d in pts])),
            'yoy_median': round(med(yoy), 2) if yoy else '',
            'resi_n': len(resi),
            'resi_price_median': int(med([d['tsubo'] for d in resi])) if resi else '',
            'comm_n': len(comm),
            'comm_price_median': int(med([d['tsubo'] for d in comm])) if comm else '',
            'points_json': json.dumps(pts[:8], ensure_ascii=False, separators=(',', ':')),
        })
    rows.sort(key=lambda r: r['station'])

    cols = ['station', 'pt_n', 'price_median', 'yoy_median',
            'resi_n', 'resi_price_median', 'comm_n', 'comm_price_median', 'points_json']
    guard(rows, a.out, minimum=400)
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader(); w.writerows(rows)

    print(f'地点 {len(ft):,} → {len(rows)}駅（駅から{a.max_dist}m以内）→ {a.out}')
    v = sorted(rows, key=lambda r: -r['price_median'])
    print('  坪単価上位:', [(r['station'], f"{r['price_median']/10000:,.0f}万/坪", f"n={r['pt_n']}") for r in v[:4]])
    if site:
        print(f'  サイトの駅 {sum(len(v) for v in site.values())} / うち公示地価あり {len(rows)}')
    if unmatched:
        u = sorted(unmatched.items(), key=lambda kv: -kv[1])
        print(f'  突合できなかった最寄駅名 {len(u)}件（都県外・島嶼部のバス停なら正常）:',
              [k for k, _ in u[:12]])
    y = [r for r in rows if r['yoy_median'] != '']
    print(f'  前年比あり {len(y)}駅  中央値 {st.median([r["yoy_median"] for r in y]):+.2f}%')


if __name__ == '__main__':
    main()
