#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""年別推移のアーカイブ(output/station_yearly.csv)に、確定した年を足していく。

  python3 yearly_update.py --archive output/station_yearly.csv \
                           --incoming input/mlit --store input/mlit_quarters

なぜこの形か
  CIが持っているのは直近ファイル(例: 2025Q2〜2026Q1)だけで、1本のファイルに
  暦年1年ぶん(Q1〜Q4)がそろうことはない。四半期ごとの取引データを
  input/mlit_quarters/ に貯め、ある年の4四半期がそろった時点で集計して
  アーカイブに1年ぶん追記する。追記した年の四半期ファイルは消す
  (定常状態では最大でも1年ぶん弱しか持たない)。

  アーカイブに入った年は二度と書き換えない。国交省の取引データは確定後に
  変わらないので、年を重ねるごとに積み上がるだけになる。

処理
  1 input/mlit の各CSVを「期 × 系統(中古マンション / 宅地)」に分け、
    まだアーカイブに無い年の期だけを store に書く(既にある期は触らない)
  2 store で4四半期そろった「年 × 系統」を集計し、アーカイブに追記
  3 追記した年の四半期ファイルを削除

  集計は station_yearly.build_rows() を呼ぶ。定義を二重に持たないため。
"""
import argparse
import glob
import os
import re
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import station_yearly as SY  # noqa: E402

# 系統 → 種類。宅地は「土地」と「土地と建物」が同じダウンロードファイルに入る。
GROUP = {'中古マンション等': 'mansion', '宅地(土地)': 'takuchi', '宅地(土地と建物)': 'takuchi'}
GROUP_ATYPES = {'mansion': {'mansion'}, 'takuchi': {'land', 'house'}}
PERIOD = re.compile(r'(20\d\d)年第(\d)四半期')


def qfile(store, year, q, group):
    return os.path.join(store, f'{year}Q{q}_{group}.csv')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--archive', default='output/station_yearly.csv')
    ap.add_argument('--incoming', default='input/mlit')
    ap.add_argument('--store', default='input/mlit_quarters')
    a = ap.parse_args()
    SY.SP = SY.load_pipeline()
    os.makedirs(a.store, exist_ok=True)

    arc = pd.read_csv(a.archive, dtype=str, keep_default_na=False) if os.path.exists(a.archive) \
        else pd.DataFrame(columns=SY.COLS)
    done = {(r['atype'], r['year']) for _, r in arc.iterrows()}
    done_groups = {(g, y) for g, ats in GROUP_ATYPES.items()
                   for y in {y for (_, y) in done} if all((t, y) in done for t in ats)}
    print(f'アーカイブ {len(arc):,}行  確定済みの年: {sorted({y for _, y in done})}')

    # 1 四半期ごとに貯める
    files = sorted(glob.glob(os.path.join(a.incoming, '*.csv')))
    written = 0
    for f in files:
        df = SY.read_csv(f)
        if '種類' not in df.columns or '取引時期' not in df.columns:
            continue
        df['_g'] = df['種類'].map(GROUP)
        for (period, g), part in df[df['_g'].notna()].groupby(['取引時期', '_g']):
            m = PERIOD.match(str(period))
            if not m:
                continue
            y, q = m.group(1), m.group(2)
            if (g, y) in done_groups:
                continue                      # 確定済みの年は触らない
            path = qfile(a.store, y, q, g)
            if os.path.exists(path):
                continue                      # 同じ期は最初に貯めたものを使う(確定後は変わらない)
            part.drop(columns=['_g']).to_csv(path, index=False, encoding='utf-8-sig')
            written += 1
            print(f'  保存 {os.path.basename(path)}  {len(part):,}行')
    if not written:
        print('  新しい四半期なし')

    # 2 4四半期そろった年を集計して追記
    have = {}
    for p in glob.glob(os.path.join(a.store, '*.csv')):
        m = re.match(r'(20\d\d)Q(\d)_(\w+)\.csv$', os.path.basename(p))
        if m:
            have.setdefault((m.group(3), m.group(1)), {})[m.group(2)] = p
    new_rows, remove = [], []
    for (g, y), qs in sorted(have.items()):
        if (g, y) in done_groups:
            remove += list(qs.values())       # 念のため。確定済みの年の残骸は消す
            continue
        if set(qs) != {'1', '2', '3', '4'}:
            print(f'  待機 {g:<8}{y}  Q{"".join(sorted(qs))} の{len(qs)}期(4期そろうまで待つ)')
            continue
        raw = pd.concat([SY.read_csv(qs[q]) for q in '1234'], ignore_index=True)
        raw['_year'] = y
        rows = SY.build_rows(raw)
        rows = [r for r in rows if (r['atype'], y) not in done]
        new_rows += rows
        remove += list(qs.values())
        print(f'  確定 {g:<8}{y}  {len(raw):,}行 → {len(rows):,}駅×種別')

    if new_rows:
        add = SY.to_frame(new_rows).astype(str)
        out = pd.concat([arc, add], ignore_index=True)
        # 既存の年を上書きしていないことを確かめてから書く
        if len(out.drop_duplicates(['station', 'atype', 'year'])) != len(out):
            sys.exit('中止: 確定済みの年と重なる行ができました。アーカイブは書き換えません。')
        out = out.sort_values(['station', 'atype', 'year']).reindex(columns=SY.COLS)
        out.to_csv(a.archive, index=False, encoding='utf-8')
        print(f'→ {a.archive}  {len(arc):,}行 → {len(out):,}行')
    for p in remove:
        os.remove(p)
        print(f'  削除 {os.path.basename(p)}')


if __name__ == '__main__':
    main()
