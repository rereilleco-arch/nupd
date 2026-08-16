#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FUDOSAN DB から市区町村別の中古マンション価格推移を取得する。

  export FUDOSANDB_API_KEY="発行キー"
  python3 price_trends_update.py --master input/tokyost_1.csv --out output/muni_price_trends.csv

なぜ必要か
  現行スコアの構成要素（坪単価・人口変化率・地価前年比・賃料）は互いに強く相関し、
  実質的に「坪単価の言い換え」になっていた。650駅を独自に序列化する新しい軸として、
  価格の時系列（キャピタル方向）を入れる。既存の land_yoy は「公示地価の前年比」だが、
  こちらは「中古マンション成約単価の長期推移」で、サイトの主題そのもの。

粒度について
  市区町村単位。駅単位ではないが、これは仕様。
  「港区にあるという事実が足立区より優位」という加点思想と同じ枠組みで、
  区の実績を所属する全駅に配る。

出力（1行=1市区町村）
  muni, muni_code, q_count, first_q, last_q,
  chg_1y, chg_3y, chg_5y, chg_10y, chg_all   … median単価の変化率(%)
  cagr_10y                                   … 年率(%)
  vol                                        … 前期比変化率の標準偏差(%)＝価格の荒さ
  cnt_chg_5y                                 … 取引件数の5年変化率(%)
  trend_json                                 … [{y,q,v,n}] 直近40四半期。スパークライン用
"""
import os, sys, csv, json, time, argparse, statistics as st

import requests

BASE = "https://fudosandb.jp/v1"
KEY = os.environ.get("FUDOSANDB_API_KEY", "")
# 既存の fudosandb_update.py と同じくパスパラメータ形式（land-price-trends/{code} と同型）。
# 環境によって命名が違う可能性があるので候補を順に試し、最初に通ったものを使い回す。
PATH_CANDIDATES = ["price-trends/{code}", "price-trends", "market/price-trends/{code}"]
PATH_OK = None
PROPERTY = "condo"

def _data(r):
    try: j = r.json()
    except Exception: return None
    return j.get("data", j) if isinstance(j, dict) else j

TOKYO = {
    "千代田区":"13101","中央区":"13102","港区":"13103","新宿区":"13104","文京区":"13105",
    "台東区":"13106","墨田区":"13107","江東区":"13108","品川区":"13109","目黒区":"13110",
    "大田区":"13111","世田谷区":"13112","渋谷区":"13113","中野区":"13114","杉並区":"13115",
    "豊島区":"13116","北区":"13117","荒川区":"13118","板橋区":"13119","練馬区":"13120",
    "足立区":"13121","葛飾区":"13122","江戸川区":"13123","八王子市":"13201","立川市":"13202",
    "武蔵野市":"13203","三鷹市":"13204","青梅市":"13205","府中市":"13206","昭島市":"13207",
    "調布市":"13208","町田市":"13209","小金井市":"13210","小平市":"13211","日野市":"13212",
    "東村山市":"13213","国分寺市":"13214","国立市":"13215","福生市":"13218","狛江市":"13219",
    "東大和市":"13220","清瀬市":"13221","東久留米市":"13222","武蔵村山市":"13223","多摩市":"13224",
    "稲城市":"13225","羽村市":"13227","あきる野市":"13228","西東京市":"13229",
    "西多摩郡瑞穂町":"13303","西多摩郡日の出町":"13305","西多摩郡檜原村":"13307","西多摩郡奥多摩町":"13308",
}

def _try(tpl, code):
    url = f"{BASE}/" + tpl.format(code=code)
    params = {"property_type": PROPERTY}
    if "{code}" not in tpl:
        params["municipality_code"] = code
    r = requests.get(url, headers={"X-API-Key": KEY}, params=params, timeout=60)
    return r

def fetch(code, tries=3):
    global PATH_OK
    for i in range(tries):
        try:
            tpls = [PATH_OK] if PATH_OK else PATH_CANDIDATES
            last = None
            for tpl in tpls:
                r = _try(tpl, code)
                last = r
                if r.status_code == 200:
                    PATH_OK = tpl
                    return _data(r)
                if r.status_code in (429, 500, 502, 503):
                    time.sleep(3 * (i + 1)); last = None; break
            if last is not None:
                sys.stderr.write(f"  {code}: HTTP {last.status_code} {last.url}\n")
                return None
        except requests.RequestException as e:
            sys.stderr.write(f"  {code}: {e}\n"); time.sleep(3 * (i + 1))
    return None

def series(rows):
    """[(year, quarter, median単価, 件数)] を時系列順に。medianが無い期は落とす。"""
    out = []
    for r in rows or []:
        v = r.get("median_price_per_m2")
        if not v:
            continue
        out.append((int(r["trade_year"]), int(r["trade_quarter"]), float(v),
                    int(r.get("transaction_count") or 0)))
    out.sort(key=lambda x: (x[0], x[1]))
    return out

def pct(a, b):
    """b→a の変化率(%)。四半期単体はぶれるので呼び出し側で平滑済みの値を渡すこと。"""
    return None if not b else round((a / b - 1) * 100, 1)

def smooth(s, i, w=4):
    """iを終点とする直近w期の中央値。四半期の揺れを均す。"""
    seg = [x[2] for x in s[max(0, i - w + 1):i + 1]]
    return st.median(seg) if seg else None

def build(rows):
    s = series(rows)
    if len(s) < 8:
        return None
    last = len(s) - 1
    cur = smooth(s, last)
    d = {"q_count": len(s),
         "first_q": f"{s[0][0]}Q{s[0][1]}", "last_q": f"{s[-1][0]}Q{s[-1][1]}"}
    for lab, back in (("1y", 4), ("3y", 12), ("5y", 20), ("10y", 40)):
        j = last - back
        d[f"chg_{lab}"] = pct(cur, smooth(s, j)) if j >= 3 else None
    d["chg_all"] = pct(cur, smooth(s, 3))
    if d.get("chg_10y") is not None:
        d["cagr_10y"] = round(((1 + d["chg_10y"] / 100) ** (1 / 10) - 1) * 100, 2)
    else:
        d["cagr_10y"] = None
    qoq = [(s[i][2] / s[i-1][2] - 1) * 100 for i in range(1, len(s)) if s[i-1][2]]
    d["vol"] = round(st.pstdev(qoq), 1) if len(qoq) > 4 else None
    n_cur = sum(x[3] for x in s[-4:])
    n_old = sum(x[3] for x in s[-24:-20]) if len(s) >= 24 else 0
    d["cnt_chg_5y"] = pct(n_cur, n_old) if n_old else None
    d["trend_json"] = json.dumps(
        [{"y": y, "q": q, "v": int(v), "n": n} for y, q, v, n in s[-40:]],
        ensure_ascii=False, separators=(",", ":"))
    return d


def guard(out, path, minimum=1, ratio=0.8):
    """出力が痩せていたら書かずに異常終了する。

    2026-08-16、FUDOSAN DB APIが GitHub Actions から 403/404 を返し、
    0件のまま output/muni_price_trends.csv を上書きした。ヘッダだけの
    111バイトになり、それをプラグインが取得して全駅から価格推移が消えた。
    失敗が「空のCSV」という正常な形で伝播したため、誰も気づかなかった。

    データ取得の失敗は、古いデータを残したままRUNを赤くする方が安全。
    """
    if len(out) < minimum:
        sys.exit(f'中止: 取得できたのが {len(out)} 件です。'
                 f'{path} は上書きしません（取得元の障害を疑ってください）。')
    if os.path.exists(path):
        with open(path, encoding='utf-8-sig') as f:
            prev = max(0, sum(1 for _ in f) - 1)
        if prev and len(out) < prev * ratio:
            sys.exit(f'中止: 既存 {prev} 件 → 今回 {len(out)} 件と大きく減りました。'
                     f'{path} は上書きしません。意図した減少なら {path} を先に消してください。')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="output/muni_price_trends.csv")
    ap.add_argument("--sleep", type=float, default=0.5)
    a = ap.parse_args()
    if not KEY:
        sys.exit("環境変数 FUDOSANDB_API_KEY が未設定です。")
    cols = ["muni","muni_code","q_count","first_q","last_q",
            "chg_1y","chg_3y","chg_5y","chg_10y","chg_all","cagr_10y","vol",
            "cnt_chg_5y","trend_json"]
    out, ng = [], []
    for name, code in TOKYO.items():
        d = build(fetch(code))
        if not d:
            ng.append(name); continue
        d["muni"], d["muni_code"] = name, code
        out.append({k: d.get(k) for k in cols})
        time.sleep(a.sleep)
    guard(out, a.out, minimum=40)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(out)
    print(f"{len(out)}/{len(TOKYO)} 市区町村 → {a.out}")
    if ng:
        print("  取得できず:", "、".join(ng))
    ok = [r for r in out if r["chg_10y"] is not None]
    if ok:
        v = sorted(ok, key=lambda r: -r["chg_10y"])
        print("\n10年変化率 上位5:", [(r["muni"], f"{r['chg_10y']:+.0f}%") for r in v[:5]])
        print("10年変化率 下位5:", [(r["muni"], f"{r['chg_10y']:+.0f}%") for r in v[-5:]])

if __name__ == "__main__":
    main()
