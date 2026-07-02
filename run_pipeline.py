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
    stats = sp.build(d1, d2, 'output/station_prices.csv',
        period_label='最新四半期(入力CSVに準拠)',
        source_label='国土交通省 不動産情報ライブラリ（不動産取引価格情報・成約価格情報）',
        updated=time.strftime('%Y-%m'))
    print('集計完了:', stats)

if __name__ == '__main__':
    main()
