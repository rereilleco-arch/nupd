#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REIT公式サイトが持っている物件データから、番地までの住所（と座標）を取る。

  python3 fetch_official_addresses.py --out reit_official_addresses.csv

なぜ必要か
  駅から近い順に並べるREIT物件の距離は、住所の粒度で精度が決まる。
  有報の所在地が「東京都品川区上大崎」（町名まで）や「東京都中央区」（区まで）しか
  ない法人があり、町や区の中心から測るので数百mずれる。
  実例：エスティメゾン白金台は町の中心から0.2kmだが、番地から測ると0.51km
  （公式の物件説明は「目黒駅徒歩約7分」）。

  1棟マンションの表に出る973物件のうち番地まで分かるのは783物件で、
  残りは次の3法人に集中していた（2026-10 実測）。3法人とも公式サイトの裏で
  物件データを1ファイル（またはページ内のデータ）で持っており、番地まで入っている。

    積水ハウス・リート     /ja/portfolio/detail-data.json     address2_J（番地）
    ＫＤＸ不動産           /ja/portfolio/detail-data.json     location（番地）
    野村不動産マスターF    /ja/portfolio/detail.html に埋め込み  address（番地）・position（緯度経度）

  物件ごとのページを巡回せず1回の取得で全件が取れるので、壊れにくく負荷も小さい。

出力（1行=1物件）
  reit_name, property_name, address, lat, lon, source
  lat/lon は公式が座標を持っている法人だけ（野村MF）。無ければ空で、
  geocode_reit.py が住所から位置を出す。

壊れたときの扱い
  サイトの形が変わって取れる件数が前回の8割を下回ったら、その法人は前回の値を残し、
  終了コード1で止める（他の処理と同じ「空のデータで上書きしない」方針）。
"""
import argparse, csv, json, os, re, sys, time, unicodedata, urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (noitas data pipeline)'}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8-sig', 'ignore')


def clean(s):
    s = re.sub(r'<[^>]+>', '', str(s or ''))
    s = s.replace('\\n', '').replace('\n', '')
    return re.sub(r'[\s　]+', '', unicodedata.normalize('NFKC', s)).strip()


def sekisui():
    d = json.loads(get('https://www.sekisuihouse-reit.co.jp/ja/portfolio/detail-data.json'))
    for v in d.values():
        if isinstance(v, dict) and v.get('name') and v.get('address2_J'):
            yield v['name'], clean(v['address2_J']), '', ''


def kdx():
    d = json.loads(get('https://www.kdx-reit.com/ja/portfolio/detail-data.json'))
    for v in d.values():
        if isinstance(v, dict) and v.get('name') and v.get('location'):
            yield v['name'], clean(v['location']), '', ''


def nomura():
    # 詳細ページ1枚に全物件のデータ（'0001': {...}）が埋め込まれている
    h = get('https://www.nre-mf.co.jp/ja/portfolio/detail.html?id=0001')
    blocks = re.split(r"\n\s*'(\d{4})'\s*:\s*\{", h)
    for i in range(1, len(blocks), 2):
        b = blocks[i + 1]
        nm = re.search(r"\bname'\s*:\s*'([^']*)'", b)
        ad = re.search(r"\baddress'\s*:\s*'([^']*)'", b)
        ps = re.search(r"\bposition'\s*:\s*'([^']*)'", b)
        if not (nm and ad):
            continue
        lat = lon = ''
        if ps:
            nums = re.findall(r'-?\d+\.\d+', ps.group(1))
            if len(nums) >= 2 and 20 < float(nums[0]) < 50 and 120 < float(nums[1]) < 155:
                lat, lon = nums[0], nums[1]
        yield nm.group(1), clean(ad.group(1)), lat, lon


SOURCES = {
    '積水ハウス・リート投資法人': (sekisui, 'https://www.sekisuihouse-reit.co.jp/ja/portfolio/'),
    'ＫＤＸ不動産投資法人': (kdx, 'https://www.kdx-reit.com/ja/portfolio/'),
    '野村不動産マスターファンド投資法人': (nomura, 'https://www.nre-mf.co.jp/ja/portfolio/'),
}
COLS = ['reit_name', 'property_name', 'address', 'lat', 'lon', 'source']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='reit_official_addresses.csv')
    a = ap.parse_args()

    prev = {}
    if os.path.exists(a.out):
        with open(a.out, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                prev.setdefault(r['reit_name'], []).append(r)

    rows, failed = [], []
    for reit, (fn, src) in SOURCES.items():
        try:
            got = [dict(zip(COLS, (reit, n, ad, la, lo, src))) for n, ad, la, lo in fn()]
        except Exception as e:
            got, err = [], str(e)[:80]
        else:
            err = ''
        old = prev.get(reit, [])
        if old and len(got) < len(old) * 0.8:
            failed.append(f'{reit}: 前回{len(old)}件 → 今回{len(got)}件 {err}')
            rows += old                       # 前回の値を残す
        else:
            rows += got
        print(f'  {reit}: {len(got)}件（番地まで {sum(1 for r in got if re.search(r"[0-9]+(番|号|-)", r["address"]))}件・座標 {sum(1 for r in got if r["lat"])}件）')
        time.sleep(1)

    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader(); w.writerows(rows)
    print(f'{len(rows)}件 → {a.out}')
    if failed:
        sys.exit('中止（前回の値を残した法人あり）: ' + ' / '.join(failed))


if __name__ == '__main__':
    main()
