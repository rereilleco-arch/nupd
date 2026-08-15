#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""棟データを自動更新する。GitHub Actions から四半期で叩く想定。

  python3 mansion_update.py --out output/mansions.csv

自動で取りに行くもの（HTMLの表から抽出）
  東京とどまるマンション情報登録簿   … 都のポータル。名称・所在地・防災の星
  中央区 防災対策優良マンション認定   … 番地まで入る
  棟別PDFの「住宅の種別」            … 分譲/賃貸。一覧HTMLには無い

手動で置くもの（APIも一括DLも無いため自動化できない）
  input/kankyo/*.csv
    東京都 建築物環境計画書制度システムで
      地域=都内全域 / 届出状態=完了 / 用途=住宅等
    で検索してCSVを書き出す。年1回。毎年300件ほど増える。

出典表記について
  制度名は出力にもサイトにも出さない。同種サイトがどこも使っていない取得元で、
  制度名を書くとそのまま再現手順を渡すことになる。表示は
  「東京都および区市が公表している情報」に留める（プラグイン側で実装済み）。
"""
import csv, json, os, re, sys, time, argparse, unicodedata

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {'User-Agent': 'Mozilla/5.0 (compatible; noitas-bot/1.0)'}
TODOMARU = 'https://www.mansion-tokyo.metro.tokyo.lg.jp/bousai/05lcp-list/'
TODOMARU_PDF = 'https://www.mansion-tokyo.metro.tokyo.lg.jp/pdf/17tokyo-lcp/01lcp-list/{id}.pdf'
CHUO = 'https://www.city.chuo.lg.jp/a0011/bousaianzen/bousai/bousaitaisaku/kousoujuutaku/kousoubosaininteiseidoichiran.html'


def get(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=40)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or 'utf-8'
                return r.text
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    sys.stderr.write(f'  取得できず: {url}\n')
    return ''


def tables(html):
    """<tr> ごとにセルの配列を返す"""
    out = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S):
        cells = [unicodedata.normalize(
            'NFKC',
            re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', '', c)).replace('&nbsp;', ' ')
        ).strip() for c in re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', tr, re.S)]
        if cells:
            out.append((tr, cells))
    return out


def fetch_todomaru():
    """一覧HTMLから 登録番号・名称・所在地・星 を取る"""
    html = get(TODOMARU)
    rows = []
    for raw, c in tables(html):
        if not re.search(r'lcp-list/[^"]+\.pdf', raw) or len(c) < 3:
            continue
        rows.append({'id': c[0], '名称': c[1], '所在地': c[2].replace(' ', ''),
                     '防災星': c[4] if len(c) > 4 else ''})
    return rows


def fetch_todomaru_types(ids, cache_dir, sleep=0.7):
    """棟別PDFから住宅の種別(分譲/賃貸)を取る。
    実測の開示率: 種別 99.8% / 竣工年月日 0% / 戸数 25% / 階数 13%。
    竣工年は「申請者の希望により非公開」が大半で取れないため、種別だけを目的にする。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.stderr.write('  pypdf 未導入のため種別の取得をスキップ\n')
        return {}
    os.makedirs(cache_dir, exist_ok=True)
    kinds = {}
    for rid in ids:
        p = os.path.join(cache_dir, rid + '.pdf')
        if not os.path.exists(p) or os.path.getsize(p) < 1000:
            try:
                r = requests.get(TODOMARU_PDF.format(id=rid), headers=UA, timeout=30)
                if r.status_code != 200:
                    continue
                open(p, 'wb').write(r.content)
                time.sleep(sleep)
            except requests.RequestException:
                continue
        try:
            t = re.sub(r'\s+', ' ', '\n'.join(
                (pg.extract_text() or '') for pg in PdfReader(p).pages))
        except Exception:
            continue
        m = re.search(r'住宅の種別\s*([^\s]{0,12})', t)
        if m:
            h = re.search(r'(分譲賃貸併存|分譲賃貸|分譲|賃貸)', m.group(1))
            if h:
                kinds[rid] = h.group(1)
    return kinds


def fetch_chuo():
    html = get(CHUO)
    rows = []
    for _, c in tables(html):
        if len(c) >= 3 and c[0].isdigit() and c[1]:
            rows.append({'名称': c[1], '所在地': '中央区' + c[2]})
    return rows


def load_kankyo(indir):
    """建築物環境計画書のCSV（手動で置く）。ヘッダ行の位置が可変なので探す"""
    out = []
    if not os.path.isdir(indir):
        return out
    for fn in sorted(os.listdir(indir)):
        if not fn.lower().endswith('.csv'):
            continue
        rows = list(csv.reader(open(os.path.join(indir, fn), encoding='utf-8-sig')))
        hi = next((i for i, r in enumerate(rows) if len(r) > 5 and '建物名' in r), None)
        if hi is None:
            continue
        H = rows[hi]
        idx = {k: H.index(k) for k in ('建物名', '所在地', '地域', '延べ面積', '階数、構造',
                                       '工事完了(予定)年月', '建築主') if k in H}
        use_i = [i for i, h in enumerate(H) if h == '用途']
        for r in rows[hi + 1:]:
            if len(r) != len(H) or not any(r):
                continue
            g = lambda k: r[idx[k]] if k in idx else ''
            ks = g('階数、構造')
            fl = re.search(r'地上(\d+)階', ks)
            st = re.search(r'、([^、]+造)', ks)
            out.append({
                '名称': g('建物名'), '所在地': g('所在地'), '区市町村': g('地域'),
                '用途': (r[use_i[0]] if use_i else ''),
                '延べ面積㎡': g('延べ面積'), '地上階': fl.group(1) if fl else '',
                '構造': st.group(1) if st else '',
                '竣工年月': (g('工事完了(予定)年月') or '')[:7],
                '建築主': re.split(r'[\s　]|代表', g('建築主'))[0],
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kankyo', default=os.path.join(HERE, '..', '..', '..',
                                                     'input', 'kankyo'))
    ap.add_argument('--pdf-cache', default=os.path.join(HERE, 'pdfs'))
    ap.add_argument('--skip-pdf', action='store_true', help='種別の取得を省く(前回結果を流用)')
    ap.add_argument('--out-dir', default=os.path.dirname(HERE))
    a = ap.parse_args()

    td = fetch_todomaru()
    print(f'とどまる登録簿      {len(td):4d}棟')
    ch = fetch_chuo()
    print(f'中央区 防災認定      {len(ch):4d}棟')
    kk = load_kankyo(a.kankyo)
    print(f'建築物環境計画書(手動) {len(kk):4d}棟  ← {a.kankyo}')
    if not kk:
        sys.stderr.write('  ※ input/kankyo が空です。年1回の手動配置が必要です\n')

    types = {}
    if not a.skip_pdf and td:
        types = fetch_todomaru_types([r['id'] for r in td], a.pdf_cache)
        print(f'住宅の種別を取得     {len(types):4d}/{len(td)}')

    base = a.out_dir
    with open(os.path.join(base, 'seed_todomaru.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['登録番号', '名称', '所在地', '区市町村', '防災星', '出典'])
        w.writeheader()
        for r in td:
            mu = re.match(r'^(.+?[区市町村])', r['所在地'])
            w.writerow({'登録番号': r['id'], '名称': r['名称'], '所在地': r['所在地'],
                        '区市町村': mu.group(1) if mu else '', '防災星': r['防災星'],
                        '出典': 'とどまるマンション'})
    with open(os.path.join(base, 'todomaru_types.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['登録番号', '名称', '種別'])
        w.writeheader()
        for r in td:
            if types.get(r['id']):
                w.writerow({'登録番号': r['id'], '名称': r['名称'], '種別': types[r['id']]})
    with open(os.path.join(base, 'seed_chuo_bousai.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['名称', '所在地', '区市町村', '出典'])
        w.writeheader()
        for r in ch:
            w.writerow({**r, '区市町村': '中央区', '出典': '中央区防災認定'})
    if kk:
        with open(os.path.join(base, 'seed_kankyokeikakusho.csv'), 'w', newline='', encoding='utf-8-sig') as f:
            cols = ['名称', '所在地', '区市町村', '用途', '延べ面積㎡', '地上階', '構造', '竣工年月', '建築主', '出典']
            w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            w.writeheader()
            for r in kk:
                w.writerow({**r, '出典': '建築物環境計画書'})
    print('\n種データを更新しました。続けて join_station.py → normalize.py → build_plugin_data.py')


if __name__ == '__main__':
    main()
