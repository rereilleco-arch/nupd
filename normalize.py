# -*- coding: utf-8 -*-
"""棟マスタの名寄せ。join_station.py の出力を受けて、公開に耐える名称だけを残す。

やること
  1. 「仮称」を含む棟を落とす
     竣工前の届出名で、竣工後に正式名称へ変わる。その名前のマンションは存在せず
     検索もされない。同一物件が住居表示と地番で二重登録される原因にもなっている。
  2. 表示名から末尾の工事語（新築工事 等）を除去
  3. 正規化名 + 区市町村 で統合
     建築物環境計画書（属性が豊富）を主、とどまる/中央区認定を従にマージする。
  4. 用途は推定しない
     とどまる・中央区認定には用途欄が無い。以前は「分譲」と決め打ちしていたが、
     プライムアーバン（賃貸の一棟収益）を分譲として出してしまった。
     todomaru_types.csv（各棟PDFの「住宅の種別」）があれば当て、無ければ空のまま。
"""
import csv, re, os, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)

# 先頭が仮称 = 名称そのものが未確定（例「(仮称)港区元麻布一丁目計画」）→ 落とす
KARI_HEAD = re.compile(r'^[\s　]*[（(]?仮称[）)]?')
# 末尾が「〜計画 / 〜プロジェクト / 〜PJ」も、正式名が決まる前の届出名
KARI_TAIL = re.compile(r'(計画|プロジェクト|ＰＪ|PJ)[\s　]*$')
# 正式名の後ろに届出上の呼称が併記されている
#   例「三田ガーデンヒルズ((仮称)港区三田一丁目計画 敷地1)」→「三田ガーデンヒルズ」
# 括弧ごと落として正式名だけ残す。ここを一律除外すると実在の大型物件が消える。
# 入れ子（(仮称)が内側）があるので、変化しなくなるまで繰り返す。
ADMIN_PAREN = re.compile(r'[（(][^（()）]*(?:仮称|計画|プロジェクト|敷地|新築|工事)[^（()）]*[）)]')
WORK = re.compile(r'[\s　]*(新築工事|建設工事|新営工事|新築|工事)[\s　]*$')

# 公営住宅は市場外。家賃も価格も市場の需給で決まらず、相場ページに混ぜると数字が壊れる。
# 建替えも延べ2,000㎡超なら建築物環境計画書の届出対象になるため、明示的に落とす。
PUBLIC = re.compile(r'(^[都区市町村]営|都営|区営|市営|町営|村営|公社住宅|公団|都民住宅|トミンハイム|コーシャハイム|UR賃貸)')

def is_placeholder(s):
    """名称が丸ごと届出上の仮の呼称か"""
    s = unicodedata.normalize('NFKC', s or '')
    if KARI_HEAD.match(s):
        return True
    # 括弧と末尾の工事語を除いた本体が「〜計画」で終わる（例「台東区根岸5丁目計画 新築工事」）
    body = WORK.sub('', ADMIN_PAREN.sub('', s).strip()).strip()
    return bool(KARI_TAIL.search(body))

def is_public_housing(s):
    return bool(PUBLIC.search(unicodedata.normalize('NFKC', s or '')))

def disp_name(s):
    """表示用の名称。併記された届出呼称と末尾の工事語を落とす"""
    s = unicodedata.normalize('NFKC', s or '').strip()
    for _ in range(4):
        t = ADMIN_PAREN.sub('', s)
        if t == s:
            break
        s = t
    s = WORK.sub('', s)
    s = re.sub(r'[（(][\s　]*[）)]', '', s)
    return re.sub(r'\s{2,}', ' ', s).strip()

def norm_key(s):
    """突合用キー。表記ゆれを潰す"""
    s = disp_name(s)
    s = re.sub(r'[（(].*?[）)]', '', s)
    s = re.sub(r'[\s・,，、／/－―ー\-‐–—_]', '', s)
    return s.lower()

def load(path):
    p = os.path.join(BASE, path)
    if not os.path.exists(p):
        return []
    with open(p, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))

def richness(r):
    """情報量。多いほうを主レコードに採用する"""
    return sum(1 for k in ('用途','竣工年月','地上階','構造','延べ面積㎡','建築主') if (r.get(k) or '').strip())

def main():
    rows = load('mansion_station_link.csv')
    total = len(rows)

    # 1) 仮称を落とす
    kept = [r for r in rows if not is_placeholder(r['名称'])]
    dropped_kari = total - len(kept)
    before = len(kept)
    kept = [r for r in kept if not is_public_housing(r['名称'])]
    dropped_public = before - len(kept)

    # 4) 用途の外部辞書（とどまるPDF由来）
    types = {}
    for r in load('todomaru_types.csv'):
        k = norm_key(r.get('名称',''))
        t = (r.get('種別') or '').strip()
        if k and t:
            types[k] = t

    # 2)(3) 名寄せ
    merged = {}
    for r in kept:
        r = dict(r)
        r['名称'] = disp_name(r['名称'])
        k = (norm_key(r['名称']), r.get('区市町村',''))
        if k not in merged:
            merged[k] = r
            merged[k]['_srcs'] = {r['出典']}
            continue
        cur = merged[k]
        cur['_srcs'].add(r['出典'])
        if richness(r) > richness(cur):           # 情報量の多い方を主に
            srcs = cur['_srcs']
            for f in ('防災星',):                  # 従から拾う値
                if not (r.get(f) or '').strip() and (cur.get(f) or '').strip():
                    r[f] = cur[f]
            merged[k] = r
            merged[k]['_srcs'] = srcs
        else:
            for f in ('用途','竣工年月','地上階','構造','延べ面積㎡','建築主','防災星'):
                if not (cur.get(f) or '').strip() and (r.get(f) or '').strip():
                    cur[f] = r[f]

    out = []
    for r in merged.values():
        r['出典'] = '／'.join(sorted(r.pop('_srcs')))
        if not (r.get('用途') or '').strip():      # 推定しない。辞書に有ればだけ当てる
            r['用途'] = types.get(norm_key(r['名称']), '')
        out.append(r)
    out.sort(key=lambda r: (r['最寄駅'], int(r['徒歩分'] or 999), r['名称']))

    cols = ['名称','所在地','区市町村','lat','lng','最寄駅','徒歩分','徒歩15分圏の駅',
            '出典','用途','延べ面積㎡','地上階','構造','竣工年月','建築主','防災星']
    with open(os.path.join(BASE,'mansion_master.csv'),'w',newline='',encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader(); w.writerows(out)

    from collections import Counter
    print(f'入力 {total}')
    print(f'  仮称で除外    -{dropped_kari}')
    print(f'  公営住宅で除外 -{dropped_public}')
    print(f'  名寄せで統合 -{len(kept)-len(out)}')
    print(f'出力 {len(out)}  → mansion_master.csv')
    print('  用途:', dict(Counter((r['用途'] or '(未確定)') for r in out)))

if __name__ == '__main__':
    main()
