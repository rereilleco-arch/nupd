#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""有報の個別物件明細から「住所」を取り、あわせて年間賃料の開示基準を判定する。

なぜやるか
  (1) 住所
      物件表の所在地は7割が「東京都渋谷区」までしか書かれておらず、座標を物件名から
      推定していた。その結果カスタリア原宿(実際は渋谷区千駄ケ谷三丁目55番3号、原宿駅
      徒歩8分)を原宿駅の座標そのもの=0.0kmに置いていた。個別物件明細には番地まで
      載っているので、そこを読めば推定をやめられる。

  (2) 年間賃料の開示基準(マスターリース)
      大和ハウスリートの注5はこう書いている。
        「年間賃料」は…賃貸借契約書に表示された月間賃料…を12倍…ただし、マスター
        リース会社とパススルー型マスターリース契約が締結されている場合、…マスター
        リース会社とエンドテナントとの間で締結されている各転貸借契約書に表示された
        月間賃料…を12倍…した金額を記載しています。
      つまり同じ列に「エンド基準」と「ML基準」が混在する。パススルー型ならエンド
      テナントの転貸賃料そのものなので使える。固定賃料型ならML会社がREITに払う額で、
      エンドの実勢より低く出るため賃料指標としては外すべき。
      テナント数では区別できない(注4が「マスターリース契約があればテナント数は1」と
      定義しているため)。判定は法人ごとの注記を読むしかない。

出力
  csv/reit_detail_locations.csv   reit_name, property_name, address, granularity
  csv/reit_ml_basis.csv           reit_name, doc_id, basis, evidence

  python3 reit_detail_scan.py --out-dir ../csv
"""
import argparse
import csv
import os
import re
import sys
from collections import Counter

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit('bs4 が必要です。/usr/local/bin/python3 で実行してください。')

CHOUME = re.compile(r'[一二三四五六七八九十〇\d０-９]+丁目')
ADDR_RX = re.compile(r'[都道府県].*?[市区町村]')
# 個別物件明細で住所が入っている行の見出し。住居表示を優先し、無ければ地番。
# 「所在地→住居表示→値」と2段になる法人(スターツ)があるので '住居表示' 単独も入れる。
ADDR_LABELS = ('所在地（住居表示）', '住居表示', '住所', '所在地', '地番')
NAME_LABELS = ('物件名称', '物件名')
# 物件名の頭に付く物件番号（T-001 / RE-020 / Ｃ－2 / O-01 など）。全角ハイフン・全角空白あり。
CODE_HEAD = re.compile(r'^[A-Za-zＡ-Ｚａ-ｚ]{1,3}[-－‐]?\d{1,4}[\s　\xa0]+')
# 「物件名：プロシード東陽町」のように1セルに見出しと値が入る形(スターツ)
NAME_INLINE = re.compile(r'^物件名[称]?[：:]\s*(.+)$')
COLON_ONLY = re.compile(r'^[：:]$')

# マスターリースの種別。これが物件単位で開示されているので、法人ごとの注記を
# 読み分ける必要はない。
#   パス・スルー型 … ML会社がエンドから受け取る転貸賃料がそのまま有報の賃料になる
#   賃料保証型/固定 … ML会社がREITに払う保証賃料が有報の賃料になる(エンドより低い)
ML_PASS = re.compile(r'パス[・･]?スルー')
ML_FIXED = re.compile(r'賃料保証|固定賃料|賃料固定')

PASSTHRU = re.compile(r'パス[・･]?スルー型マスターリース')
FIXED = re.compile(r'賃料固定型マスターリース|固定賃料型マスターリース|賃料保証型マスターリース')


def norm_key(s):
    """見出しの表記ゆれを吸収する。ＭＬ種類/ML種別/ M L 種 別 を同一視する。"""
    s = re.sub(r'[\s　\xa0]', '', str(s or ''))
    return s.translate(str.maketrans(
        'ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ０１２３４５６７８９',
        'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'))


def load_env():
    p = os.path.expanduser('~/NOITAS/4-データ/.env')
    if os.path.exists(p):
        for line in open(p, encoding='utf-8'):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# 物件名の前後に付く飾り。三井不動産アコモは明細の見出しを「１．大川端賃貸棟」
# 「10．パークアクシス大塚」と連番付きで書く。末尾の「（注）」も落とす。
NUM_HEAD = re.compile(r'^[0-9０-９]{1,3}[．.、,][\s　]*')
NOTE_TAIL = re.compile(r'（注[^）]*）\s*$')


def clean_name(s):
    s = re.sub(r'[\s　\xa0]+', '', str(s or ''))
    s = NUM_HEAD.sub('', s)
    s = NOTE_TAIL.sub('', s)
    return CODE_HEAD.sub('', s).strip()


def granularity(addr):
    if CHOUME.search(addr):
        return '丁目'
    if re.search(r'\d', addr):
        return '番地'
    return '町名'


# 表外に物件名を置く法人の3つの形
#   ADR            T-001　レジディア島津山
#   平和不動産リート  物件番号：Of-64 物件名称：ＨＦ上野稲荷町ビルディング（注１）
#   三井不動産アコモ  パークアクシス押上レジデンス   ← 接頭辞なしの素の物件名
NAME_LABELED = re.compile(r'物件名[称]?[：:]\s*([^（(]+)')
SENTENCE = re.compile(r'[、。]|^（注|^\(注|以下|ため|します|ません|基づき')


def preceding_name(tb):
    """表の直前テキストから物件名を取る。直前を数行遡り、最初に名前と読める行を返す。"""
    el = tb
    for _ in range(8):
        el = el.find_previous(string=True)
        if el is None:
            return None
        t = re.sub(r'[\s　\xa0]+', ' ', str(el)).strip()
        if not t or len(t) > 60:
            continue
        m = NAME_LABELED.search(t)
        if m and len(m.group(1).strip()) >= 2:
            return m.group(1).strip()
        m = CODE_HEAD.match(t)
        if m and len(t[m.end():].strip()) >= 2:
            return t[m.end():].strip()
        # 素の物件名。文らしい要素が無く短い行だけを採る(誤検出を避けるため厳しめ)
        if len(t) <= 30 and not SENTENCE.search(t) and '：' not in t and '|' not in t:
            return t
    return None


def scan_details(soup):
    """個別物件明細の表から {物件名: {住所, ML種別, ML会社, PM会社, 戸数, 建築時期}} を作る。

    明細は1物件1表で、見出しと値が交互に並ぶ。表の形は法人ごとに3通りある。
      大和ハウスリート  物件番号 | カスタリア原宿 | … | 住所 | 東京都渋谷区千駄ケ谷三丁目…
      コンフォリア      物件名称 | コンフォリア日本橋人形町 | … | 所在地 | 東京都中央区…
      ADR              (表の外)T-001 レジディア島津山 → 表は 所在地 から始まる
    どの形でも「見出し→次のセルが値」なので、対で辞書にしてから拾う。
    """
    out = {}
    for tb in soup.find_all('table'):
        cells = [c.get_text(' ', strip=True) for c in tb.find_all(['td', 'th'])]
        if not cells:
            continue
        d = {}
        for i in range(len(cells) - 1):
            k = norm_key(cells[i])
            if not k:
                continue
            # 「見出し｜：｜値」と区切りのコロンが1セル入る形(三井不動産アコモ)
            v = cells[i + 1].strip()
            if COLON_ONLY.match(v):
                v = cells[i + 2].strip() if i + 2 < len(cells) else ''
            if v:
                d.setdefault(k, v)
        addr = None
        for label in ADDR_LABELS:
            v = d.get(norm_key(label))
            if v and ADDR_RX.search(v):
                addr = v
                break
        if not addr:
            continue
        name = None
        for label in NAME_LABELS:
            v = d.get(norm_key(label))
            if v and len(v) >= 2 and not v.isdigit():
                name = v
                break
        if not name:
            v = d.get('物件番号')
            if v and len(v) >= 2 and not re.fullmatch(r'[\dO-Z一二三四五六七八九十０-９]+', v):
                name = v
        if not name:
            # 「物件名：プロシード東陽町」形式(スターツ)
            for c in cells[:8]:
                m = NAME_INLINE.match(c.strip())
                if m and len(m.group(1)) >= 2:
                    name = m.group(1)
                    break
        if not name:
            # 先頭セルが「O-01　多摩センタートーセイビル」形式(トーセイ)
            head = re.sub(r'[\s　\xa0]+', ' ', cells[0].strip())
            m = CODE_HEAD.match(head)
            if m and len(head[m.end():].strip()) >= 2:
                name = head[m.end():].strip()
        if not name:
            name = preceding_name(tb)
        if not name:
            continue
        # 「（既存棟）」等が続く複合表記や注記は落とす
        addr = re.split(r'（|\(|\s{2,}', addr)[0].strip()
        ml = next((d[k] for k in ('ML種別', 'ML種類', 'マスターリース種別', 'マスターリースの種別')
                   if d.get(k)), '')
        key = clean_name(name)
        if len(key) < 2:
            continue
        out.setdefault(key, {
            'address': addr,
            'ml_type': ml,
            'ml_company': d.get('ML会社', ''),
            'pm_company': d.get('PM会社', ''),
            'units': d.get('賃貸可能戸数', ''),
            'built': d.get('建築時期', ''),
        })
    return out


def ml_class(ml_type):
    """ML種別の表記を パススルー / 保証・固定 / 不明 に寄せる。"""
    s = str(ml_type or '')
    if not s:
        return ''
    if ML_PASS.search(s):
        return 'パススルー'
    if ML_FIXED.search(s):
        return '保証・固定'
    return '不明'


def scan_ml_basis(text):
    """年間賃料の注記から開示基準を判定する。

    戻り値: ('パススルー' | '固定' | '両方記載' | '不明', 根拠の抜粋)
    「パススルー型…の場合はエンドテナントの転貸賃料」と書いてあれば、その法人は
    パススルー型物件についてはエンド基準。固定型の物件はML基準のまま混ざるので、
    両方の語が出る法人は '両方記載' として個別に見る必要がある。
    """
    # 年間賃料の注記本体を探す(「年間賃料」と「月間賃料」が同じ段落にある箇所)
    seg = ''
    for m in re.finditer(r'「年間賃料」', text):
        s = text[m.start():m.start() + 700]
        if '月間賃料' in s or '月額賃料' in s:
            seg = s
            break
    if not seg:
        return '不明', ''
    p, f = bool(PASSTHRU.search(seg)), bool(FIXED.search(seg))
    if p and f:
        return '両方記載', seg[:300]
    if p:
        return 'パススルー', seg[:300]
    if f:
        return '固定', seg[:300]
    # マスターリースに言及が無い = エンドとの契約をそのまま書いている
    return 'ML言及なし', seg[:300]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--props', default='csv/reit_properties.csv')
    ap.add_argument('--out-dir', default='csv')
    ap.add_argument('--only', nargs='*', help='EDINETのdocIDを限定する(検証用)')
    a = ap.parse_args()
    load_env()
    key = os.environ.get('EDINET_API_KEY')
    if not key:
        sys.exit('EDINET_API_KEY が未設定です。')
    # reit_parser は同じディレクトリに置く
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import reit_parser as RP

    # 対象は住宅物件を持つ法人の最新有報(reit_properties.csv の doc_id)
    targets = {}
    for r in csv.DictReader(open(a.props, encoding='utf-8-sig')):
        if r.get('doc_id'):
            targets.setdefault((r['reit_name'], r['doc_id']), 0)
            targets[(r['reit_name'], r['doc_id'])] += 1
    order = sorted(targets.items(), key=lambda x: -x[1])
    if a.only:
        order = [x for x in order if x[0][1] in a.only]

    locs, basis = [], []
    for (reit, doc), n in order:
        try:
            htmls = RP.fetch_honbun_htmls(doc, key)
        except Exception as e:                        # noqa: BLE001
            print(f'  [失敗] {reit} {doc}: {type(e).__name__}: {e}', flush=True)
            basis.append({'reit_name': reit, 'doc_id': doc, 'basis': '取得失敗', 'evidence': str(e)[:200]})
            continue
        det, b, ev = {}, '不明', ''
        for h in htmls:
            soup = BeautifulSoup(h, 'lxml')
            det.update(scan_details(soup))
            if b == '不明':
                b2, ev2 = scan_ml_basis(soup.get_text(' ', strip=True))
                if b2 != '不明':
                    b, ev = b2, ev2
        for name, v in det.items():
            locs.append({'reit_name': reit, 'property_name': name,
                         'address': v['address'], 'granularity': granularity(v['address']),
                         'ml_type': v['ml_type'], 'ml_class': ml_class(v['ml_type']),
                         'ml_company': v['ml_company'], 'pm_company': v['pm_company'],
                         'units': v['units'], 'built': v['built']})
        basis.append({'reit_name': reit, 'doc_id': doc, 'basis': b, 'evidence': ev})
        mlc = Counter(ml_class(v['ml_type']) for v in det.values())
        print(f'  {reit[:24]:<26} 物件{n:>4}  明細{len(det):>4}件  注記={b:<8} '
              f'ML種別={dict(mlc)}', flush=True)

    os.makedirs(a.out_dir, exist_ok=True)
    p1 = os.path.join(a.out_dir, 'reit_detail_locations.csv')
    with open(p1, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['reit_name', 'property_name', 'address', 'granularity',
                                          'ml_type', 'ml_class', 'ml_company', 'pm_company',
                                          'units', 'built'])
        w.writeheader(); w.writerows(locs)
    p2 = os.path.join(a.out_dir, 'reit_ml_basis.csv')
    with open(p2, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['reit_name', 'doc_id', 'basis', 'evidence'])
        w.writeheader(); w.writerows(basis)

    print(f'\n→ {p1}  {len(locs)}件')
    for k, v in Counter(x['granularity'] for x in locs).most_common():
        print(f'   {k:<6}{v:>5}件')
    print(f'→ {p2}  {len(basis)}法人')
    for k, v in Counter(x['basis'] for x in basis).most_common():
        print(f'   {k:<10}{v:>4}法人')


if __name__ == '__main__':
    main()
