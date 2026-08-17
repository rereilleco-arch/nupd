#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""区市町村ごとの集計を作る。区ページ(/ward/<区名>/)の元データ。

なぜ区ページを作るか
  「港区 マンション 相場」「世田谷区 土地 価格」は駅名クエリより検索需要が大きいのに、
  サイトは1ページも持っていない。50ページだけなので薄くならず、
  ランキング→区→駅という内部リンクの中間層にもなる。

なぜ区単位で意味があるのか
  駅ページに区単位の値を出すと「同じ区の駅に同じ値を配っているだけ」になる。
  だが区ページでは区単位が本来の粒度であって、薄めた値ではない。
  とくに1棟(共同住宅)の取引は駅あたり中央値2.4件しかなく駅では割れないが、
  区なら23区中21区で8件以上あり、価格帯別の分布が成立する。

出力 ward_stats.csv (1行=1市区町村)
  muni, muni_code, station_n
  mansion_n, mansion_tsubo_median, mansion_price_median      … 中古マンション
  land_n, land_tsubo_median                                  … 土地
  house_n, house_price_median                                … 土地建物(戸建等)
  isshu_n, isshu_bands_json                                  … 1棟(共同住宅)の価格帯別
  lp_n, lp_price_median, lp_yoy_median, lp_comm_median       … 公示地価
  mansion_bldg_n                                             … 棟マスタの棟数
"""
import argparse, csv, glob, json, os, re, statistics as st, sys, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get('NOITAS_DIR') or (os.path.dirname(HERE) if os.path.basename(HERE) == '_pipeline' else HERE)
TSUBO = 3.305785

# 1棟の価格帯。3億で切ると木造が571件中27件しか残らない（＝構造で切る必要がない）。
# 帯を分けるだけで除外しないので、恣意性が入らない。
BANDS = [(0, 1e8, '1億円未満'), (1e8, 3e8, '1〜3億円'),
         (3e8, 10e8, '3〜10億円'), (10e8, float('inf'), '10億円以上')]


def num(v):
    try:
        return float(str(v).replace(',', ''))
    except (TypeError, ValueError):
        return None


def med(v):
    return round(st.median(v)) if v else ''


def tsubo(r):
    """専有坪単価。中古マンションの行には坪単価も㎡単価も入っていないので
    総額と面積から出す（宅地の行には坪単価が入っている）。"""
    p, a = num(r.get('取引価格（総額）')), num(r.get('面積（㎡）'))
    return p / (a / TSUBO) if p and a and a > 0 else None


def find(*names):
    """リポジトリ直下 / input/ / 手元のNOITAS構成 の順に探す"""
    for n in names:
        for d in (BASE, os.path.join(BASE, 'input'), HERE, os.path.join(BASE, '_dist'),
                  os.path.join(BASE, '..', 'NOITAS基本データ', 'csv'),
                  os.path.join(BASE, 'output')):
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return None


def read_mlit(paths):
    rows = []
    for p in paths:
        for enc in ('cp932', 'utf-8-sig', 'utf-8'):
            try:
                with open(p, encoding=enc) as f:
                    rows += list(csv.DictReader(f))
                break
            except (UnicodeDecodeError, LookupError):
                continue
    return rows


def isshu_bands(rows):
    """1棟(共同住宅)を価格帯で分ける。除外はしない。
       成約価格情報は戸建てなので対象外（MLITの注記による）。"""
    ik = [r for r in rows
          if '共同住宅' in str(r.get('用途') or '')
          and '成約' not in str(r.get('価格情報区分') or '')]
    out = []
    for lo, hi, lab in BANDS:
        s = [r for r in ik if lo <= (num(r.get('取引価格（総額）')) or -1) < hi]
        if not s:
            continue
        pr = [num(r.get('取引価格（総額）')) for r in s]
        fl = [x for x in (num(r.get('延床面積（㎡）')) for r in s) if x]
        out.append({'k': lab, 'n': len(s), 'price': med(pr), 'floor': med(fl)})
    return len(ik), json.dumps(out, ensure_ascii=False, separators=(',', ':'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mlit', default='', help='取引CSVのglob。既定は input/mlit/*.csv')
    ap.add_argument('--l01', default=os.path.join(HERE, 'l01'))
    ap.add_argument('--out', default=os.path.join(BASE, 'ward_stats.csv'))
    a = ap.parse_args()

    files = sorted(glob.glob(a.mlit)) if a.mlit else sorted(glob.glob(os.path.join(BASE, 'input', 'mlit', '*.csv')))
    if not files:
        files = sorted(glob.glob(os.path.join(BASE, '..', 'NOITAS基本データ', 'csv', 'mlit', '*', '*.csv')))
    if not files:
        sys.exit('中止: 取引CSVが見つかりません（input/mlit/*.csv）。')
    rows = read_mlit(files)
    print(f'取引 {len(rows):,}件  ← {len(files)}ファイル')

    # 駅→区。区ごとの駅数を数えるのに使う
    stn_muni = {}
    p = find('fudosan_enrichment.csv')
    if p:
        with open(p, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                if r.get('fudosan_muni'):
                    stn_muni[r['station']] = r['fudosan_muni']

    # 棟マスタ。所在地の先頭が区市町村名
    bldg = {}
    p = find('mansions.csv')
    if p:
        with open(p, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                m = re.match(r'^(.+?[区市町村])', unicodedata.normalize('NFKC', r.get('addr') or ''))
                if m:
                    bldg[m.group(1)] = bldg.get(m.group(1), 0) + 1

    # 公示地価。L01は市区町村コードを持つので、駅を経由せず直接集計できる
    lp = {}
    g = glob.glob(os.path.join(a.l01, '*', '*.geojson'))
    if g:
        for ft in json.load(open(g[0], encoding='utf-8'))['features']:
            q = ft['properties']
            price, code = num(q.get('L01_008')), str(q.get('L01_001') or '')
            if not price or not code:
                continue
            zone = str(q.get('L01_051') or '')
            lp.setdefault(code, []).append(
                (round(price * TSUBO), num(q.get('L01_009')),
                 any(k in zone for k in ('商業', '近商', '準工', '工業', '工専'))))

    by = {}
    for r in rows:
        mu = (r.get('市区町村名') or '').strip()
        if mu:
            by.setdefault(mu, []).append(r)

    out = []
    for mu, g in sorted(by.items()):
        code = (g[0].get('市区町村コード') or '').strip()
        man = [r for r in g if '中古マンション' in str(r.get('種類') or '')]
        land = [r for r in g if r.get('種類') == '宅地(土地)']
        house = [r for r in g if r.get('種類') == '宅地(土地と建物)']
        n_is, bands = isshu_bands(g)
        pts = lp.get(code, [])
        out.append({
            'muni': mu, 'muni_code': code,
            'station_n': sum(1 for v in stn_muni.values() if v == mu),
            'mansion_n': len(man),
            'mansion_tsubo_median': med([x for x in (tsubo(r) for r in man) if x]),
            'mansion_price_median': med([x for x in (num(r.get('取引価格（総額）')) for r in man) if x]),
            'land_n': len(land),
            'land_tsubo_median': med([x for x in (num(r.get('坪単価')) for r in land) if x]),
            'house_n': len(house),
            'house_price_median': med([x for x in (num(r.get('取引価格（総額）')) for r in house) if x]),
            'isshu_n': n_is, 'isshu_bands_json': bands,
            'lp_n': len(pts),
            'lp_price_median': med([p for p, _, c in pts if not c]),
            'lp_comm_median': med([p for p, _, c in pts if c]),
            'lp_yoy_median': round(st.median([y for _, y, _ in pts if y is not None]), 2) if any(y is not None for _, y, _ in pts) else '',
            'mansion_bldg_n': bldg.get(mu, 0),
        })

    if len(out) < 30:
        sys.exit(f'中止: {len(out)} 市区町村しか集計できませんでした。入力CSVを確認してください。')

    cols = list(out[0].keys())
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader(); w.writerows(out)

    print(f'{len(out)} 市区町村 → {a.out}')
    print(f'  駅の対応 {len(stn_muni)}駅 / 棟マスタ {sum(bldg.values())}棟 / 公示地価 {sum(len(v) for v in lp.values())}地点')
    v = sorted(out, key=lambda r: -(r['mansion_tsubo_median'] or 0))
    print('  マンション坪単価 上位:', [(r['muni'], f"{r['mansion_tsubo_median']/10000:,.0f}万") for r in v[:4] if r['mansion_tsubo_median']])
    print('  1棟が8件以上ある市区町村:', sum(1 for r in out if r['isshu_n'] >= 8))


if __name__ == '__main__':
    main()
