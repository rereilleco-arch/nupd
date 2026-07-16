#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REIT物件を市区町村ごとに集約する
================================
run_reits.py が出力した reit_properties.csv を読み、東京の市区町村ごとに
  - 鑑定還元利回り(cap rate)の中央値   ← 開示物件のみで計算
  - 物件数                              ← 鑑定評価額がある物件をカウント
  - 代表事例JSON(物件名・cap・鑑定評価額) ← 駅ページ表示用
を集計し、reit_by_muni.csv を出力する。

駅ページの既存ACFフィールドに対応:
  reit_cap_median  → 周辺REIT 鑑定還元利回り中央値(%)
  reit_count       → 周辺REIT物件数
  reit_examples    → 周辺REIT事例JSON

【設計方針(ユーザー合意済み)】
- 利回りは「鑑定採用還元利回り」(CaprateMapの鑑定CRと同じ)。NOI利回りではないので
  表示ラベルは「鑑定還元利回り」とする。
- cap rate中央値は cap_rate が取れた物件のみで計算(非開示物件は除外)。
- 物件数は鑑定評価額がある物件をカウント(cap rate非開示でも物件は存在するため)。
- 東京の23区+市部のみ対象(noitasは東京の不動産DB)。
"""
import csv
import json
import re
import statistics
import argparse

# 東京23区
TOKYO23 = ['千代田区', '中央区', '港区', '新宿区', '文京区', '台東区', '墨田区', '江東区',
           '品川区', '目黒区', '大田区', '世田谷区', '渋谷区', '中野区', '杉並区', '豊島区',
           '北区', '荒川区', '板橋区', '練馬区', '足立区', '葛飾区', '江戸川区']

# ---------------------------------------------------------------- 住宅判定
# 駅ページのファーストビューは区分マンション相場(住宅)なので、周辺REITも住宅に揃える。
# (オフィス等の全用途データは reit_properties.csv に残り、REIT個別ページで使える)
# 判定は use_type 優先、空欄時は物件名ブランド → REIT名 の順にフォールバックする。
RESI_USE = re.compile(r'居住|住宅|レジデン|共同住宅')
NONRESI_USE = re.compile(r'オフィス|事務所|商業|物流|ホテル|宿泊|倉庫|店舗|事業所|底地|その他')
NONRESI_NAME = re.compile(r'Dプロジェクト|ロジ|物流|ロジスティ|DPL|プロロジス|GLP|オフィス|'
                          r'センタービル|モール|アウトレット|ショッピング|ホテル')
RESI_NAME = re.compile(r'レジデン|レジデンス|ハイツ|コーポ|メゾン|コンフォリア|プラウド|'
                       r'パークアクシス|アクシス|カーサ|ヴィラ|ガーデンホームズ|'
                       r'S-FORT|S-RESIDENCE|プロシード|アルティザ')
RESI_REIT = re.compile(r'レジデンシャル|アコモデーション|コンフォリア|リビング|プロシード|'
                       r'サムティ・レジデン')


def is_residential(r):
    use = (r.get('use_type', '') or '').strip()
    name = r.get('property_name', '') or ''
    reit = r.get('reit_name', '') or ''
    if use:
        if RESI_USE.search(use):
            return True
        if NONRESI_USE.search(use):
            return False
    # 用途空欄: 物件名ブランド → REIT名 の順で判定
    if NONRESI_NAME.search(name):
        return False
    if RESI_NAME.search(name):
        return True
    if RESI_REIT.search(reit):
        return True
    return False   # 判定不能は住宅に入れない(誤混入を防ぐ)


def extract_tokyo_muni(location, region):
    """所在地(優先)またはregionから東京の市区町村名を抽出。東京外はNoneを返す。"""
    text = (location or '')
    # 23区を直接探す(「東京都港区…」「港区…」どちらでも)
    for w in TOKYO23:
        if w in text:
            return w
    # 東京都の市部(「東京都八王子市」等)
    m = re.search(r'東京都([^\s]+?市)', text)
    if m:
        return m.group(1)
    # locationが空/東京外表記でも、regionが東京を示すなら23区総体として扱えないので None
    # (region は「東京23区」等の粗い区分でしかなく、個別区に落とせないため集約対象外)
    return None


def extract_town(location):
    """住所から町名(丁目の手前まで)を抽出する。次フェーズの近接並び替え用。
    例: '東京都港区北青山二丁目13番5号' -> '北青山'
        '東京都中央区銀座4-9-13' -> '銀座'
    抽出できなければ空文字。"""
    if not location:
        return ''
    s = str(location)
    # 「東京都○○区」を除去
    s = re.sub(r'^.*?[都道府県].*?[区市町村]', '', s)
    # 丁目・数字の手前までを町名とする
    m = re.match(r'([^\d０-９一二三四五六七八九十]+?)(?:[一二三四五六七八九十\d０-９]+丁目|[\d０-９]|$)', s)
    if m:
        return m.group(1).strip()
    return s[:6].strip()


def to_float(s):
    try:
        return float(s) if s not in (None, '') else None
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='infile', default='reit_properties.csv')
    ap.add_argument('--out', default='reit_by_muni.csv')
    ap.add_argument('--examples', type=int, default=5, help='事例JSONに載せる物件数')
    ap.add_argument('--all-uses', action='store_true',
                    help='住宅フィルタを外して全用途で集約(既定は住宅のみ)')
    args = ap.parse_args()

    with open(args.infile, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))

    # 既定は住宅のみ(駅ページ用)。--all-uses で全用途。
    if not args.all_uses:
        rows = [r for r in rows if is_residential(r)]

    # 市区町村ごとに物件を集める
    by_muni = {}
    for r in rows:
        muni = extract_tokyo_muni(r.get('location'), r.get('region'))
        if not muni:
            continue
        by_muni.setdefault(muni, []).append(r)

    out_rows = []
    for muni, props in sorted(by_muni.items()):
        # cap rate中央値(開示物件のみ)
        caps = [to_float(p.get('cap_rate')) for p in props]
        caps = [c for c in caps if c is not None]
        cap_median = round(statistics.median(caps), 2) if caps else ''

        # 物件数(鑑定評価額がある物件)
        with_appraisal = [p for p in props if to_float(p.get('appraisal_value')) is not None]
        count = len(with_appraisal)

        # 事例は同区の住宅物件を「全件」持たせる(表示件数の制限はプラグイン側)。
        # これにより、次フェーズで町名・距離ベースの並び替えを入れる際、
        # データ側を作り直さずプラグインの表示ロジックだけで対応できる。
        # 並びは鑑定評価額の大きい順(cap rate開示を優先)。
        def sort_key(p):
            has_cap = 1 if to_float(p.get('cap_rate')) is not None else 0
            av = to_float(p.get('appraisal_value')) or 0
            return (has_cap, av)
        # JSONキーはプラグインの既存表示コード(eki_reit_table)が読む日本語キーに合わせる:
        #   物件 / REIT / 取得百万 / 鑑定百万 / NOI利回り
        # (プラグイン側の表示コードを変更せずに済む)
        examples = []
        for p in sorted(with_appraisal, key=sort_key, reverse=True):
            examples.append({
                '物件': p.get('property_name', ''),
                'REIT': p.get('reit_name', ''),
                '取得百万': to_float(p.get('acquisition_price')),
                '鑑定百万': to_float(p.get('appraisal_value')),
                'NOI利回り': to_float(p.get('cap_rate')),
                '稼働率': to_float(p.get('occupancy')),
                '町名': extract_town(p.get('location', '')),   # 次フェーズの近接並び替え用
            })

        out_rows.append({
            'muni': muni,
            'reit_cap_median': cap_median,
            'reit_count': count,
            'reit_cap_disclosed': len(caps),   # cap rateが取れた物件数(参考)
            'reit_examples': json.dumps(examples, ensure_ascii=False),
        })

    with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['muni', 'reit_cap_median', 'reit_count',
                                          'reit_cap_disclosed', 'reit_examples'])
        w.writeheader()
        w.writerows(out_rows)

    # サマリ
    print(f"集約完了: {len(out_rows)}市区町村 -> {args.out}")
    print(f"{'市区町村':<10}{'物件数':>6}{'cap開示':>7}{'還元利回り中央値':>16}")
    for r in out_rows:
        cm = f"{r['reit_cap_median']}%" if r['reit_cap_median'] != '' else '—'
        print(f"{r['muni']:<10}{r['reit_count']:>6}{r['reit_cap_disclosed']:>7}{cm:>16}")


if __name__ == '__main__':
    main()
