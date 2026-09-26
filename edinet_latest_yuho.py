#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""各REITの「最新の有価証券報告書」のdocIDを取り直す。

なぜ必要か
  edinet_reit_matches.csv が持っているのは「最新のdocID」であって
  「最新の有報のdocID」ではない。発行登録書や訂正届出書が後から出ると
  そちらが最新になり、run_reits.py が物件表を取りに行けずスキップする。
  実測では60法人中15法人以上がこの状態で、コンフォリア175物件・
  積水ハウス139物件・ユナイテッドアーバン145物件が丸ごと落ちていた。

やること
  EDINETの書類一覧APIを日付で遡り、formCode=07B000（有価証券報告書・
  内国投資証券）だけを拾って、EDINETコードごとに最新を残す。
  edinet_reit_recon.py と同じ「1日ずつ舐める」方式。APIは日付指定しか
  受け付けず、会社指定で検索できないため。

  export EDINET_API_KEY=xxxx   （または ~/NOITAS/4-データ/.env）
  python3 edinet_latest_yuho.py --days 420 --out edinet_latest_yuho.csv
"""
import argparse, csv, datetime as dt, os, sys, time
import requests   # urllib は環境によってSSL検証に失敗する(証明書チェーンに自己署名が入る)。
                  # requests は certifi を使うため通る。リポジトリ内の他スクリプトも requests。

API = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
YUHO_FORM = "07B000"          # 有価証券報告書（内国投資証券）
ORDINANCE = "030"             # 特定有価証券の内容等の開示に関する内閣府令

def load_env():
    p = os.path.expanduser('~/NOITAS/4-データ/.env')
    if os.path.exists(p):
        for line in open(p, encoding='utf-8'):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def fetch_day(day, key, tries=3, strict=False):
    """1日分の書類一覧を返す。strict=True のときは失敗を握り潰さず例外を上げる。

    取得失敗を [] で返すと「エラーは出ないが1件も取れない」という
    最も分かりにくい壊れ方をする(実際にそれで15分回した)。
    呼び出し側の初回だけ strict=True にして、環境の問題なら即座に落とす。
    """
    last = None
    for i in range(tries):
        try:
            r = requests.get(API, params={"date": day.isoformat(), "type": 2,
                                          "Subscription-Key": key}, timeout=60)
            if r.status_code == 200:
                return r.json().get("results") or []
            last = f"HTTP {r.status_code}"
            if r.status_code in (429, 500, 502, 503):
                time.sleep(3 * (i + 1)); continue
            break
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(2 * (i + 1))
    if strict:
        raise RuntimeError(f"EDINET APIに接続できません（{last}）")
    return []

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=420,
                    help='今日から何日遡るか。REITの有報は半期ごとなので400日超あれば全法人を1回は拾える')
    ap.add_argument('--out', default='edinet_latest_yuho.csv')
    ap.add_argument('--sleep', type=float, default=0.25)
    a = ap.parse_args()
    load_env()
    key = os.environ.get('EDINET_API_KEY')
    if not key:
        sys.exit('EDINET_API_KEY が未設定です。')

    latest = {}
    today = dt.date.today()
    # 初回だけ strict。環境やキーの問題なら、420日無駄に回す前にここで落とす。
    probe = fetch_day(today - dt.timedelta(days=3), key, strict=True)
    print(f'  疎通確認OK（3日前の書類 {len(probe)}件）', flush=True)
    for i in range(a.days):
        d = today - dt.timedelta(days=i)
        for r in fetch_day(d, key):
            if r.get('formCode') != YUHO_FORM or r.get('ordinanceCode') != ORDINANCE:
                continue
            ec = r.get('edinetCode')
            if not ec:
                continue
            # 日付降順に見ているので、最初に見つかったものが最新
            if ec not in latest:
                latest[ec] = {'edinet_code': ec,
                              'filer_name': r.get('filerName') or '',
                              'doc_id': r.get('docID') or '',
                              'submit_date': r.get('submitDateTime') or '',
                              'description': r.get('docDescription') or ''}
        if i % 30 == 0:
            print(f'  {d} まで遡及  法人{len(latest)}件', flush=True)
        time.sleep(a.sleep)

    out = sorted(latest.values(), key=lambda x: x['filer_name'])
    with open(os.path.expanduser(a.out), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['edinet_code', 'filer_name', 'doc_id', 'submit_date', 'description'])
        w.writeheader(); w.writerows(out)
    print(f'\n→ {a.out}  {len(out)}法人ぶんの最新有報')

if __name__ == '__main__':
    main()
