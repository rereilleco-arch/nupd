#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""駅 × 年 の推移データを作る。

  python3 station_yearly.py --src '../2021~2024' '../../NOITAS基本データ/プラグイン' \
                            --out ../station_yearly.csv

なぜ作るか
  現行サイトは「今いくらか」しか答えていない。読者が本当に知りたいのは
  「その駅は伸びているのか」で、そこに答えているサイトが無い。

集計を既存パイプラインと揃える理由
  推移ページの数字と、駅ページの現在値が食い違うと信頼を失う。
  station_pipeline.py の trimmean / station_key / 除外条件をそのまま読み込んで使う。
  自前で書き直すと必ずズレる。

起点を2021年にした根拠
  MLITの「成約価格情報」(レインズ由来)は2021Q1から全四半期に入っている。
  2020年以前は「不動産取引価格情報」(アンケート由来)のみで母集団が変わるため、
  そこを跨ぐと2021年に段差が出る。実測で2021Q1から成約1,130件を確認済み。

出力（1行 = 駅 × 年 × 種別）
  year は暦年(Q1〜Q4)。4四半期そろった年だけを出す。
  station, year, atype, count,
  price_median, price_trimmean, price_min, price_max,
  tsubo_median, tsubo_trimmean, area_median, built_median, dist_median
"""
import argparse, glob, os, re, sys
import pandas as pd

# 既存の集計ロジックを読み込む。書き直さない。
def load_pipeline():
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.getcwd(), '~/Downloads/noitas-data-repo', '~/NOITAS/4-データ/NOITAS基本データ/py'):
        p = os.path.expanduser(base)
        if os.path.exists(os.path.join(p, 'station_pipeline.py')):
            sys.path.insert(0, p)
            import station_pipeline as sp
            return sp
    sys.exit('station_pipeline.py が見つかりません。--pipeline でパスを指定してください。')

SP = None

def read_csv(path):
    """MLITのCSVはcp932。年次で複数ファイルに分かれていても同じ列構造。"""
    for enc in ('cp932', 'utf-8-sig'):
        try:
            return pd.read_csv(path, encoding=enc, dtype=str, low_memory=False)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f'エンコーディングを判定できません: {path}')

def year_of(s):
    m = re.search(r'(\d{4})年', str(s) or '')
    return m.group(1) if m else None


def quarter_of(s):
    """「2025年第2四半期」→ '2'。年ごとの期の揃いを検査するために使う。"""
    m = re.search(r'第(\d)四半期', str(s) or '')
    return m.group(1) if m else None

def agg_year(d, atype):
    """station_pipeline._normalize 済みの1年分から、駅ごとの集計を作る。
    集計項目は build() の mansion/house/land 分岐と同じ定義に揃えている。"""
    w = d[d['station'].notna() & d['price'].notna() & ~d['special']]
    out = []
    for stn, g in w.groupby('station'):
        p = g['price']
        t = g['unit_tsubo'].dropna()
        r = {'station': stn, 'atype': atype, 'count': int(len(g)),
             'price_median': int(round(p.median())),
             'price_trimmean': int(round(SP.trimmean(p))),
             'price_min': int(round(p.min())), 'price_max': int(round(p.max()))}
        if len(t):
            r['tsubo_median'] = int(round(t.median()))
            r['tsubo_trimmean'] = int(round(SP.trimmean(t)))
        if g['area'].notna().any():
            r['area_median'] = int(round(g['area'].median()))
        if 'built' in g and g['built'].notna().any():
            r['built_median'] = int(round(g['built'].median()))
        if g['distmin'].notna().any():
            r['dist_median'] = int(round(g['distmin'].median()))
        out.append(r)
    return out

KIND = {'中古マンション等': 'mansion', '宅地(土地と建物)': 'house', '宅地(土地)': 'land'}

COLS = ['station', 'year', 'atype', 'count', 'price_median', 'price_trimmean',
        'price_min', 'price_max', 'tsubo_median', 'tsubo_trimmean',
        'area_median', 'built_median', 'dist_median']


def build_rows(raw):
    """年(_year)と種類ごとに駅別の集計行を作る。raw は4四半期そろった年だけにしておくこと。
    yearly_update.py からも呼ぶので、集計の定義はここ1か所に置く。"""
    rows = []
    for (yr, kind), g in raw.groupby(['_year', '種類']):
        atype = KIND.get(kind)
        if not atype:
            continue
        d = SP._normalize(g.copy())
        if atype == 'mansion':
            d['unit_tsubo'] = d.apply(
                lambda x: x['price'] / (x['area'] / SP.TSUBO) if x['area'] and x['area'] > 0 else None, axis=1)
        else:
            fl = d['延床面積（㎡）'].map(SP.num) if '延床面積（㎡）' in d else None
            tb = d['坪単価'].map(SP.num) if '坪単価' in d else None
            if atype == 'house':
                d['unit_tsubo'] = [p / (f / SP.TSUBO) if (f and f > 0 and p) else None
                                   for p, f in zip(d['price'], fl if fl is not None else [None] * len(d))]
            else:
                d['unit_tsubo'] = tb if tb is not None else None
        for r in agg_year(d, atype):
            r['year'] = yr
            rows.append(r)
    return rows


def to_frame(rows):
    out = pd.DataFrame(rows).reindex(columns=COLS).sort_values(['station', 'atype', 'year'])
    return out.where(pd.notna(out), '')


def main():
    global SP
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', nargs='+', required=True, help='MLIT CSVを含むディレクトリ')
    ap.add_argument('--out', default='station_yearly.csv')
    ap.add_argument('--from-year', default='2021')
    a = ap.parse_args()
    SP = load_pipeline()

    files = []
    for d in a.src:
        files += sorted(glob.glob(os.path.join(os.path.expanduser(d), '**', '*.csv'), recursive=True))
    if not files:
        sys.exit('CSVが見つかりません。')

    frames = []
    for i, f in enumerate(files):
        df = read_csv(f)
        if '種類' not in df.columns:
            print(f'  skip(列が違う): {os.path.basename(f)}'); continue
        df['_src'] = i
        frames.append(df)
        print(f'  読込 {os.path.basename(f):<34} {len(df):>8,}行  '
              + ' / '.join(f'{k}:{v:,}' for k, v in df['種類'].value_counts().items()))
    raw = pd.concat(frames, ignore_index=True)

    # ファイル間の重なりは「期ごとに1ファイルから採る」で解消する。
    #
    # 以前は drop_duplicates() で全行の完全一致を消していたが、MLITの行にはIDが
    # 無く、同じ棟・同じ広さ・同じ期・同じ価格の別取引は完全一致になる(中古
    # マンション2021〜2024の1ファイル内だけで994行)。サイト本体の
    # station_pipeline.py は重複を消していないので、消すと年データだけ件数が
    # 少なくなり、同じ期間で比べたとき駅ページの現在値と食い違う。
    # 重なる期(例: 2024Q4 が 2021〜2024 と 2024Q4〜2025Q1 の両方にある)は
    # 両ファイルで件数が一致することを確認済み。
    first = raw.groupby(['種類', '取引時期'])['_src'].transform('min')
    raw = raw[raw['_src'] == first]

    raw['_q'] = raw['取引時期'].map(quarter_of)
    raw['_year'] = raw['取引時期'].map(year_of)
    raw = raw[raw['_year'].notna() & (raw['_year'] >= a.from_year)]

    # 暦年(Q1〜Q4)で切り、4四半期そろっていない年を落とす。
    #
    # MLITの取引CSVは「直近1年」を四半期単位で入れ替える運用なので、最新年は
    # 期の途中までしか無いことがある(2026-09時点の直近ファイルは2025Q2〜2026Q1で、
    # 2026はQ1の1期だけ)。それを4期そろった年と並べると季節性のぶん嘘になる。
    # 年の途中までの数字は出さない。駅ページの現在値(直近4四半期)は別に出ている。
    #
    # 落とした年は必ずログに出す。黙って消えると「なぜ2026が無いのか」が
    # 分からなくなり、同じ調査をやり直すことになる。
    keep = []
    print()
    for (kind, yr), g in raw.groupby(['種類', '_year']):
        if KIND.get(kind) is None:
            continue
        qs = sorted(x for x in g['_q'].dropna().unique())
        if len(qs) == 4:
            keep.append((kind, yr))
            continue
        print(f'  除外 {KIND[kind]:<8}{yr}  Q{"".join(qs)} の{len(qs)}期しか無い ({len(g):,}行)')
    raw = raw[[(k, y) in keep for k, y in zip(raw['種類'], raw['_year'])]]
    print(f'\n対象 {len(raw):,}行  年: {sorted(raw["_year"].unique())}')

    rows = build_rows(raw)

    out = to_frame(rows)
    out.to_csv(os.path.expanduser(a.out), index=False, encoding='utf-8')
    print(f'\n→ {a.out}  {len(out):,}行')
    for t, g in out.groupby('atype'):
        yrs = sorted(g['year'].unique())
        full = g.groupby('station')['year'].count().eq(len(yrs)).sum()
        print(f'   {t:<8} 駅×年 {len(g):>6,}  全{len(yrs)}年そろう駅 {full:>4}')

if __name__ == '__main__':
    main()
