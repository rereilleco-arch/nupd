#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FUDOSAN DB API で駅マスタを拡充する(v2)。
市区町村単位の指標を取得→駅の所在市区町村に割当。駅単位の専有坪単価と掛けて利回りを算出。

  pip install requests pandas
  export FUDOSANDB_API_KEY="発行キー"   # https://fudosandb.jp/developers
  python3 fudosandb_update.py --master tokyost_1.csv --prices station_prices.csv --out fudosan_enrichment.csv

出力列(station=post_title でACFインポート):
  賃料坪単価系: rent_tsubo / rent_tsubo_low / rent_tsubo_high   ← 月額・円/坪。標準条件25㎡1K築10徒歩5分
  収益:        yield_est                                         ← 駅単位 想定表面利回り%
  人口/地価/災害: pop2050 / pop_change / land_yoy / flood_pct / landslide_pct  ← 区市町村単位
  地価推移:    land_trend_json                                   ← 直近15年 residential 円/㎡ [{y,v}]
  REIT(同区住宅): reit_cap_median / reit_count / reit_examples   ← NOIベースcap rate中央値＋事例
  メタ:        fudosan_muni / fudosan_updated / fudosan_source

定義: 賃料・人口・地価・REITは区市町村単位の推定/集計値。坪単価・利回りは駅単位。
      賃料はFUDOSAN DBのモデル推定値。将来予測は不採用(過大予測リスクのため)。
"""
import os, sys, time, argparse, re, json, statistics
import requests, pandas as pd

BASE = "https://fudosandb.jp/v1"
KEY  = os.environ.get("FUDOSANDB_API_KEY", "")
TSUBO25 = 25 / 3.305785          # 25㎡=7.5625坪
RANGE_LOW, RANGE_HIGH = 0.85, 1.05   # 実測築年感応度(築30年で約-13%)＋駅距離/階数/間取りのばらつき
STD = dict(area_m2=25, layout="1K", build_age_years=10, walk_min=5)

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

def muni_from_address(addr):
    if pd.isna(addr): return None
    s = re.sub(r'^.*?都', '', str(addr))
    m = (re.match(r'(.+?郡.+?[町村])', s) or re.match(r'(.+?[区市])', s) or re.match(r'(.+?[町村])', s))
    return m.group(1) if m else None

def _data(r):
    try: j = r.json()
    except: return None
    return j.get("data", j) if isinstance(j, dict) else j

# --- HTTP: 429/5xx は指数バックオフで再試行する ---------------------------
MAX_RETRY = 5
FAIL = {"count": 0}          # 最終的に失敗した呼び出し数(安全装置の判定に使う)

def _request(method, path, **kw):
    """429(レート制限)・5xx は待って再試行。Retry-Afterがあれば従う。"""
    url = f"{BASE}/{path}"
    wait = 2.0
    for attempt in range(MAX_RETRY):
        try:
            r = requests.request(method, url, timeout=60, **kw)
        except requests.RequestException as e:
            if attempt == MAX_RETRY - 1:
                FAIL["count"] += 1
                print(f"  ! 通信失敗 {path}: {e}")
                return None
            time.sleep(wait); wait *= 2
            continue
        if r.status_code == 200:
            return _data(r)
        if r.status_code == 429 or 500 <= r.status_code < 600:
            ra = r.headers.get("Retry-After")
            sleep_s = float(ra) if (ra and str(ra).replace('.','',1).isdigit()) else wait
            if attempt == MAX_RETRY - 1:
                FAIL["count"] += 1
                print(f"  ! HTTP {r.status_code} 再試行上限 {path}")
                return None
            print(f"  . HTTP {r.status_code} → {sleep_s:.0f}秒待機して再試行 ({path})")
            time.sleep(sleep_s); wait *= 2
            continue
        # 4xx(429以外)は再試行しても無駄
        FAIL["count"] += 1
        print(f"  ! HTTP {r.status_code} {path}")
        return None
    return None

def GET(path):
    return _request("GET", path, headers={"X-API-Key":KEY})

def POST(path, body):
    return _request("POST", path, headers={"X-API-Key":KEY,"Content-Type":"application/json"}, json=body)

def yen_tsubo(rent_yen):
    return round(rent_yen / TSUBO25)

def fetch_muni(code):
    out = {}
    ap = GET(f"area-profile/{code}")
    if ap:
        out["pop2050"]      = ap.get("population_2050")
        out["pop_change"]   = round(ap["population_change_rate"]*100,2) if ap.get("population_change_rate") is not None else None
        out["land_yoy"]     = ap.get("land_price_yoy_change")
        out["flood_pct"]    = round(ap["flood_risk_area_pct"]*100,1) if ap.get("flood_risk_area_pct") is not None else None
        out["landslide_pct"]= round(ap["landslide_risk_area_pct"]*100,1) if ap.get("landslide_risk_area_pct") is not None else None
    er = POST("estimate-rent", {"municipality_code":code, **STD})
    if er and er.get("estimated_rent_yen"):
        base = er["estimated_rent_yen"]
        out["rent_tsubo"]      = yen_tsubo(base)
        out["rent_tsubo_low"]  = yen_tsubo(base*RANGE_LOW)
        out["rent_tsubo_high"] = yen_tsubo(base*RANGE_HIGH)
        out["_rent_base"]      = base   # 利回り計算用(内部)
    # 地価推移(直近15年 residential。無ければ commercial)
    lt = GET(f"land-price-trends/{code}")
    if isinstance(lt, list) and lt:
        for cat in ("residential","commercial"):
            ser=[d for d in lt if d.get("use_category")==cat and d.get("avg_price_per_m2")]
            if ser:
                ser=sorted(ser,key=lambda d:d["year"])[-15:]
                out["land_trend_json"]=json.dumps([{"y":d["year"],"v":int(d["avg_price_per_m2"])} for d in ser],ensure_ascii=False)
                break
    # 【削除】REIT(同区住宅)の取得はEDINET版(reit_by_station.csv)に置き換わったため不要。
    # ここで取得してもACF反映時に後段のEDINET版で上書きされるだけで、API呼び出しの無駄
    # (=レート制限の一因)だったので廃止した。
    return out

code_name = {v:k for k,v in TOKYO.items()}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--master",default="tokyost_1.csv")
    ap.add_argument("--prices",default="station_prices.csv")
    ap.add_argument("--out",default="fudosan_enrichment.csv")
    ap.add_argument("--sleep",type=float,default=1.2,help="市区町村ごとの待機秒(レート制限回避)")
    ap.add_argument("--min-success",dest="min_success",type=float,default=0.7,
                    help="この成功率を下回ったらCSVを更新しない(既存温存)")
    a=ap.parse_args()
    if not KEY: sys.exit("環境変数 FUDOSANDB_API_KEY が未設定です。")

    m=pd.read_csv(a.master,encoding="utf-8-sig",dtype=str)
    m["muni"]=m["address"].map(muni_from_address)
    prices=pd.read_csv(a.prices,dtype=str)
    tsubo_map=dict(zip(prices["station"],prices.get("mansion_tsubo_trimmean",pd.Series(dtype=str))))

    cache,miss={},[]
    targets=sorted(set(m["muni"].dropna()))
    ok_n=0
    for i,name in enumerate(targets,1):
        code=TOKYO.get(name)
        if not code: miss.append(name); continue
        v=fetch_muni(code)
        cache[name]=v
        # 賃料か人口のどちらかが取れていれば「取得成功」とみなす
        if v.get("rent_tsubo") is not None or v.get("pop2050") is not None: ok_n+=1
        print(f"  [{i}/{len(targets)}] {name} … "
              f"賃料{'○' if v.get('rent_tsubo') is not None else '×'} "
              f"人口{'○' if v.get('pop2050') is not None else '×'}")
        time.sleep(a.sleep)          # レート制限回避(既定1.2秒)
    if miss: print("コード未知(スキップ):",miss)
    n_target=len([t for t in targets if TOKYO.get(t)])
    rate = ok_n/n_target if n_target else 0
    print(f"\n取得成功 {ok_n}/{n_target} 市区町村 ({rate*100:.0f}%) / API失敗 {FAIL['count']}件")

    rows=[]
    for _,r in m.iterrows():
        v=cache.get(r["muni"])
        if not v: continue
        st=r["post_title"]; d={"station":st,"fudosan_muni":r["muni"]}
        d.update({k:val for k,val in v.items() if not k.startswith("_")})
        try:
            tsubo=float(tsubo_map.get(st) or 0); base=float(v.get("_rent_base") or 0)
            if tsubo>0 and base>0:
                d["yield_est"]=round(base*12/(tsubo*TSUBO25)*100,2)
        except: pass
        d["fudosan_updated"]=time.strftime("%Y-%m")
        d["fudosan_source"]="FUDOSAN DB(国交省データ・推定/集計値) 標準条件:25㎡/1K/築10年/徒歩5分"
        rows.append(d)

    # REIT系(reit_cap_median/reit_count/reit_examples)はEDINET版CSVが担当するため出力しない
    cols=["station","rent_tsubo","rent_tsubo_low","rent_tsubo_high","yield_est",
          "pop2050","pop_change","land_yoy","flood_pct","landslide_pct","land_trend_json",
          "fudosan_muni","fudosan_updated","fudosan_source"]
    out=pd.DataFrame(rows).drop_duplicates("station").reindex(columns=cols)

    # --- 安全装置 -------------------------------------------------------
    # レート制限(429)等で取得が大きく欠けたとき、空/不完全なCSVで既存を上書きしない。
    # 上書きすると駅ページの賃料・人口が消えるため、既存CSVを温存して警告のみ出す。
    # (ワークフローを止めないよう終了コードは0。git diffが出ないので既存が保たれる)
    if rate < a.min_success or out.empty:
        print(f"\n[中止] 取得成功率 {rate*100:.0f}% が閾値 {a.min_success*100:.0f}% 未満のため、"
              f"{a.out} を更新しませんでした(既存データを温存)。")
        print("       レート制限(429)の可能性があります。時間を置いて再実行してください。")
        return

    out.to_csv(a.out,index=False,encoding="utf-8")
    print(f"出力 {len(out)}駅 → {a.out}  利回り算出:{out['yield_est'].notna().sum()}")

if __name__=="__main__":
    main()
