#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
input/mlit/ 配下の東京取引CSV(不動産情報ライブラリの一括DL)を読み、
既存 station_pipeline で駅別集計して output/station_prices.csv を生成する。
複数四半期・複数ファイルを置いてOK(全て結合して集計)。cp932/utf-8を自動判定。
"""
import glob, sys, time
import os
import pandas as pd
import station_pipeline as sp

def read_any(path):
    for enc in ('cp932','utf-8-sig','utf-8'):
        try: return pd.read_csv(path, encoding=enc, dtype=str)
        except Exception: continue
    raise RuntimeError(f'読込失敗: {path}')

def quarter_range(df):
    """取引時期の列から「2025年第2四半期〜2026年第1四半期」を作る"""
    col = next((c for c in df.columns if '取引時期' in c), None)
    if not col:
        return ''
    q = sorted(set(str(v) for v in df[col].dropna() if str(v).strip()))
    if not q:
        return ''
    return q[0] if len(q) == 1 else f'{q[0]}〜{q[-1]}'


def main():
    files = sorted(glob.glob('input/mlit/*.csv'))
    if not files: sys.exit('input/mlit/ にCSVがありません。東京の取引CSVを置いてください。')
    print('入力:', files)
    df = pd.concat([read_any(f) for f in files], ignore_index=True)
    if '種類' not in df.columns:
        sys.exit('「種類」列が見つかりません。不動産情報ライブラリの取引CSVか確認してください。')
    d1 = df[df['種類'].isin(['宅地(土地)','宅地(土地と建物)'])].copy()   # land/house
    d2 = df[df['種類']=='中古マンション等'].copy()                        # mansion
    os.makedirs('output', exist_ok=True)
    # 対象期間は実データから作る。以前は'最新四半期(入力CSVに準拠)'という
    # 内部向けの文言をそのまま出典欄に出しており、読者には意味が伝わらないうえ
    # CSVを手で入れ替えている運用が透けていた。
    period_label = quarter_range(df)
    stats = sp.build(d1, d2, 'output/station_prices.csv',
        period_label=period_label,
        source_label='国土交通省 不動産情報ライブラリ（不動産取引価格情報・成約価格情報）',
        updated=time.strftime('%Y-%m'))
    print('集計完了:', stats)

if __name__ == '__main__':
    main()
