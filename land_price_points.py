#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国土数値情報の地価公示(L01)と都道府県地価調査(L02)から、駅ごとの地価を集計する。

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
  points_json  [{addr,use,price,tsubo,yoy,dist,far,kind,asof}] 距離順に最大10地点（kind=公示/基準）
"""
import argparse, csv, glob, json, os, re, statistics as st, sys, unicodedata, urllib.request, zipfile, io as _io

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get('NOITAS_DIR') or (os.path.dirname(HERE) if os.path.basename(HERE) == '_pipeline' else HERE)
# 東京都(13)。地価公示(L01・1月1日時点)と都道府県地価調査(L02・7月1日時点)の両方を読む。
# 年は固定しない。以前は L01-24 を固定で読み、2025・2026年版が出たあとも
# 2年前の地価を「直近1年」として出し続けていた。毎回、今年から遡って最初にある版を使う。
KINDS = {
    #       種類    国土数値情報の区分  属性コード（価格円/㎡, 前年比%, 所在地, 最寄駅, 距離m, 用途地域, 指定容積率, 調査年）
    'L01': ('公示', dict(price='L01_008', yoy='L01_009', addr='L01_025', station='L01_048',
                        dist='L01_050', zone='L01_051', far='L01_058', year='L01_007')),
    'L02': ('基準', dict(price='L02_006', yoy='L02_007', addr='L02_022', station='L02_045',
                        dist='L02_046', zone='L02_047', far='L02_052', year='L02_005')),
}
URL = 'https://nlftp.mlit.go.jp/ksj/gml/data/{k}/{k}-{yy:02d}/{k}-{yy:02d}_13_GML.zip'
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


def _get(url, head=False):
    req = urllib.request.Request(url, method='HEAD' if head else 'GET', headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': 'https://nlftp.mlit.go.jp/ksj/'})
    return urllib.request.urlopen(req, timeout=120)


def latest_url(kind):
    """今年から5年遡り、最初に公開されている版のURLを返す"""
    import datetime
    y = datetime.date.today().year % 100
    for yy in range(y, y - 5, -1):
        url = URL.format(k=kind, yy=yy)
        try:
            with _get(url, head=True) as r:
                if r.status == 200 and int(r.headers.get('Content-Length') or 0) > 100000:
                    return url
        except Exception:
            continue
    sys.exit(f'中止: {kind} の公開版が見つかりません（取得元の障害を疑ってください）。')


def fetch(dst, kind):
    """最新版をダウンロードして展開し、geojsonのパスを返す。
    展開先は版ごとに分ける（古い版が残っていても新しい版を読む）"""
    url = latest_url(kind)
    ver = os.path.basename(url).split('_')[0]           # 例 L01-26
    d = os.path.join(dst, ver)
    g = glob.glob(os.path.join(d, '**', '*.geojson'), recursive=True)
    if not g:
        os.makedirs(d, exist_ok=True)
        with _get(url) as r:
            zipfile.ZipFile(_io.BytesIO(r.read())).extractall(d)
        print(f'元データを取得: {url}')
        g = glob.glob(os.path.join(d, '**', '*.geojson'), recursive=True)
    return g[0]


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
    ap.add_argument('--cache', default=os.path.join(HERE, 'l01'))   # ward_stats.py も同じ場所の L01 を読む
    ap.add_argument('--out', default=os.path.join(BASE, 'station_land_points.csv'))
    ap.add_argument('--max-dist', type=int, default=1500, help='駅からの距離の上限m')
    ap.add_argument('--stations', default='', help='サイトの駅一覧。出力の駅名をこれに揃える')
    a = ap.parse_args()

    sf = find_stations(a.stations)
    if not sf:
        sys.exit('中止: station_coords.csv が見つかりません。駅名を正規化できないため出力しません。')
    site = site_stations(sf)
    print(f'駅一覧: {sf}（{sum(len(v) for v in site.values())}駅）')

    by, unmatched, n_ft = {}, {}, 0
    for kind, (label, F) in KINDS.items():
        # 公示は ward_stats.py と共有する l01/ に、基準は l02/ に置く
        cache = a.cache if kind == 'L01' else os.path.join(os.path.dirname(a.cache), 'l02')
        ft = json.load(open(fetch(cache, kind), encoding='utf-8'))['features']
        n_ft += len(ft)
        for x in ft:
            p = x['properties']
            stn = str(p.get(F['station']) or '').strip()
            price = num(p.get(F['price']))
            dist = num(p.get(F['dist']))
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
            yr = str(p.get(F['year']) or '')
            pt = {
                'addr': re.sub(r'\s+', '', str(p.get(F['addr']) or '')),
                'use': str(p.get(F['zone']) or '').strip(),
                'price': int(price),
                'tsubo': int(round(price * TSUBO)),
                'yoy': num(p.get(F['yoy'])),
                'dist': int(dist) if dist is not None else None,
                'far': num(p.get(F['far'])),
                # 種類と時点。公示は1月1日、基準は7月1日時点の価格
                'kind': label,
                'asof': f"{yr}-01-01" if kind == 'L01' else f"{yr}-07-01",
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
            'points_json': json.dumps(pts[:10], ensure_ascii=False, separators=(',', ':')),
        })
    rows.sort(key=lambda r: r['station'])

    cols = ['station', 'pt_n', 'price_median', 'yoy_median',
            'resi_n', 'resi_price_median', 'comm_n', 'comm_price_median', 'points_json']
    guard(rows, a.out, minimum=400)
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader(); w.writerows(rows)

    print(f'地点 {n_ft:,}（公示＋基準） → {len(rows)}駅（駅から{a.max_dist}m以内）→ {a.out}')
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
