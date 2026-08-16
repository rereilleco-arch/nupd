# -*- coding: utf-8 -*-
"""mansion_master.csv → プラグイン同梱用 data/mansions.csv

列を短縮しているのはCSVのバイト数を抑えるため（プラグインが毎回パースしてtransientに載せる）。
用途が空の棟はそのまま空で出す。空欄は「分譲でも賃貸でもない」ではなく
「公表情報から確認できていない」の意味で、PHP側では分譲/賃貸どちらのリストにも出さない。
推定で埋めない（過去に「分譲」と決め打ちして賃貸の一棟収益を分譲として出す事故を起こした）。
"""
import csv, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
# 環境変数が無くてもスクリプトの置き場所から判定する（_pipeline配下なら親、直下ならそこ）
BASE = os.environ.get('NOITAS_DIR') or (os.path.dirname(HERE) if os.path.basename(HERE)=='_pipeline' else HERE)
DIST = os.path.join(BASE, '_dist')

def main():
    os.makedirs(DIST, exist_ok=True)
    src = os.path.join(BASE, 'mansion_master.csv')
    with open(src, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    # 出典ごとに「分譲住宅／分譲」「賃貸住宅／賃貸」と表記が割れるので揃える
    USE = {'分譲住宅':'分譲','賃貸住宅':'賃貸','分譲賃貸混在':'分譲・賃貸','分譲賃貸併存':'分譲・賃貸'}
    out = []
    for r in rows:
        area = (r.get('延べ面積㎡') or '').strip()
        try:
            area = str(int(float(area)))
        except ValueError:
            area = ''
        out.append({
            'name': r['名称'],
            'st': r['最寄駅'],
            'min': r['徒歩分'],
            'near': r['徒歩15分圏の駅'],
            'use': USE.get((r.get('用途') or '').strip(), (r.get('用途') or '').strip()),
            'year': (r.get('竣工年月') or '')[:4],
            'floors': re.sub(r'\D', '', r.get('地上階') or ''),
            'area': area,
            'src': r['出典'],
            'addr': r['所在地'],
        })
    dst = os.path.join(DIST, 'mansions.csv')
    with open(dst, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['name','st','min','near','use','year','floors','area','src','addr'])
        w.writeheader(); w.writerows(out)

    from collections import Counter
    print(f'{len(out)} 行 → {dst}  ({os.path.getsize(dst):,} bytes)')
    print('  用途:', dict(Counter(r['use'] or '(未確定)' for r in out)))
    print('  竣工年あり:', sum(1 for r in out if r['year']))

if __name__ == '__main__':
    main()
