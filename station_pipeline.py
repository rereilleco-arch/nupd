# -*- coding: utf-8 -*-
"""
駅ベース集計の共有パイプライン。CSVビルドもAPI自動更新も同一ロジックを使う。
入力DataFrameは「日本語カラム名」を持つこと(APIの英語キーは呼び出し側でリネーム)。
file1相当(土地/土地建物)と file2相当(中古マンション等)を渡す。
"""
import pandas as pd, numpy as np, json, re

TSUBO = 3.305785
QORDER = {'2025年第1四半期':1,'2025年第2四半期':2,'2025年第3四半期':3,'2025年第4四半期':4}
# 四半期表記は年に依存しないようパターンでも解釈
def qorder(period):
    if pd.isna(period): return 0
    m = re.search(r'第([1-4])四半期', str(period))
    return int(m.group(1)) if m else 0
BAD = ['調停・競売等', '関係者間取引', '瑕疵有りの可能性', 'その他事情有り']

def num(s):
    if pd.isna(s): return np.nan
    s = str(s).replace(',', '').replace('㎡以上','').replace('㎡未満','').replace('㎡','').replace('m','').strip()
    try: return float(s)
    except: return np.nan
def year(s):
    if pd.isna(s): return np.nan
    m = re.search(r'(\d{4})', str(s)); return float(m.group(1)) if m else np.nan
def dist(s):
    if pd.isna(s): return np.nan
    s = str(s).strip(); return float(s) if s.isdigit() else np.nan
def station_key(s):
    if pd.isna(s) or str(s).strip()=='' : return None
    s = re.sub(r'[（(].*?[)）]', '', str(s)).strip()
    return s + '駅' if s else None
def jval(x):
    if pd.isna(x): return None
    return int(round(float(x)))

def trimmean(series):
    """外れ値(Tukeyフェンス外)のみ自動除外した平均。外れ値が無ければ通常平均と一致。"""
    s = pd.to_numeric(series, errors='coerce').dropna().astype(float)
    n = len(s)
    if n == 0: return None
    if n < 4: return s.mean()
    q1, q3 = s.quantile(0.25), s.quantile(0.75); iqr = q3 - q1
    lo, hi = q1 - 1.5*iqr, q3 + 1.5*iqr
    t = s[(s >= lo) & (s <= hi)]
    return t.mean() if len(t) else s.mean()

def eff_far(designated, breadth, zoning):
    if pd.isna(designated) or designated <= 0: return np.nan
    z = '' if zoning is None or (isinstance(zoning,float) and pd.isna(zoning)) else str(zoning)
    if pd.isna(breadth) or breadth <= 0 or any(k in z for k in ['調整区域','非線引','都計外']) or z=='':
        return designated
    coef = 0.4 if '住' in z else 0.6
    return min(designated, breadth*coef*100)

def _normalize(d):
    for c in ['取引価格（総額）','面積（㎡）','建築年','最寄駅：距離（分）','最寄駅：名称',
              '価格情報区分','取引の事情等','取引時期','種類']:
        if c not in d: d[c] = np.nan
    d = d.copy()
    d['price']   = d['取引価格（総額）'].map(num)
    d['area']    = d['面積（㎡）'].map(num)
    d['built']   = d['建築年'].map(year)
    d['distmin'] = d['最寄駅：距離（分）'].map(dist)
    d['dist_raw']= d['最寄駅：距離（分）']
    d['station'] = d['最寄駅：名称'].map(station_key)
    d['kubun']   = d['価格情報区分']
    jijo = d['取引の事情等'].fillna('')
    d['special'] = jijo.apply(lambda x: any(b in x for b in BAD))
    return d

def _near_complete_sort(g, cm):
    g=g.copy(); g['_c']=cm; g['_q']=g['取引時期'].map(qorder)
    g['_d']=g['distmin'].fillna(999); g['_n']=g['distmin'].notna() & (g['distmin']<=20)
    g['_qual']=g['_c'] & g['_n']
    return g.sort_values(['_qual','_n','_c','_q','_d'], ascending=[False,False,False,False,True])

def _ex_dist(r):
    if pd.notna(r['distmin']): return jval(r['distmin'])
    return r['dist_raw'] if isinstance(r['dist_raw'],str) else None

def _examples(g, atype, n=30):
    if atype=='land':    cm=g['tsubo'].notna() & g['area'].notna()
    elif atype=='house': cm=g['floor'].notna() & g['built'].notna()
    else:                cm=g['area'].notna() & g['built'].notna()
    gg=_near_complete_sort(g,cm); out=[]
    for _,r in gg.head(n).iterrows():
        e={'価格':jval(r['price']),'時期':r['取引時期'],'区分':r['kubun'],'距離分':_ex_dist(r)}
        if atype=='land':
            e['坪単価']=jval(r['tsubo']); e['面積m2']=jval(r['area'])
            e['一種単価']=jval(r['isshu']) if pd.notna(r['isshu']) else None
            e['実効容積率']=jval(r['eff_far']) if pd.notna(r['eff_far']) else None
            e['指定容積率']=jval(r['far_designated']) if pd.notna(r['far_designated']) else None
        elif atype=='house':
            e['延床坪単価']=jval(r['floor_tsubo']); e['延床m2']=jval(r['floor'])
            e['土地m2']=jval(r['area']); e['築年']=jval(r['built'])
            e['構造']=r['建物の構造'] if pd.notna(r['建物の構造']) else None
        else:
            e['専有坪単価']=jval(r['unit_tsubo']); e['専有m2']=jval(r['area'])
            e['間取り']=r['間取り'] if pd.notna(r.get('間取り')) else None
            e['築年']=jval(r['built']); e['構造']=r['建物の構造'] if pd.notna(r.get('建物の構造')) else None
            e['改装']=r['改装'] if pd.notna(r.get('改装')) else None
        out.append(e)
    return json.dumps(out, ensure_ascii=False)

def build(df1, df2, out_path, period_label='', source_label='', updated=''):
    """df1: 土地/土地建物の生データ(JP列), df2: 中古マンション等の生データ(JP列)"""
    d1=_normalize(df1); d2=_normalize(df2)
    d1['atype']=d1['種類'].map({'宅地(土地)':'land','宅地(土地と建物)':'house'})
    d1['tsubo']=d1['坪単価'].map(num) if '坪単価' in d1 else np.nan
    d1['floor']=d1['延床面積（㎡）'].map(num) if '延床面積（㎡）' in d1 else np.nan
    d1['floor_tsubo']=np.where((d1['atype']=='house')&(d1['floor']>0), d1['price']/(d1['floor']/TSUBO), np.nan)
    d1['unit_tsubo']=np.where(d1['atype']=='land', d1['tsubo'], d1['floor_tsubo'])
    d1['far_designated']=d1['容積率（％）'].map(num) if '容積率（％）' in d1 else np.nan
    d1['breadth']=d1['前面道路：幅員（ｍ）'].map(num) if '前面道路：幅員（ｍ）' in d1 else np.nan
    zoning = d1['都市計画'] if '都市計画' in d1 else pd.Series([None]*len(d1))
    d1['eff_far']=[eff_far(a,b,z) for a,b,z in zip(d1['far_designated'],d1['breadth'],zoning)]
    d1['isshu']=np.where((d1['atype']=='land')&d1['tsubo'].notna()&d1['eff_far'].notna()&(d1['eff_far']>0),
                         d1['tsubo']/(d1['eff_far']/100.0), np.nan)

    d2=d2.copy(); d2['atype']='mansion'
    d2['unit_tsubo']=np.where(d2['area']>0, d2['price']/(d2['area']/TSUBO), np.nan)

    rows={}
    def agg(d, atype):
        w=d[d['station'].notna() & d['atype'].eq(atype) & d['price'].notna() & ~d['special']]
        for stn,g in w.groupby('station'):
            r=rows.setdefault(stn,{'station':stn}); p=g['price']; t=g['unit_tsubo'].dropna(); pre=atype
            r[f'{pre}_count']=int(len(g))
            r[f'{pre}_price_median']=int(round(p.median())); r[f'{pre}_price_mean']=int(round(p.mean()))
            r[f'{pre}_price_trimmean']=int(round(trimmean(p)))
            r[f'{pre}_price_min']=int(round(p.min())); r[f'{pre}_price_max']=int(round(p.max()))
            if len(t):
                r[f'{pre}_tsubo_median']=int(round(t.median())); r[f'{pre}_tsubo_mean']=int(round(t.mean()))
                r[f'{pre}_tsubo_trimmean']=int(round(trimmean(t)))
            r[f'{pre}_dist_median']=int(round(g['distmin'].median())) if g['distmin'].notna().any() else ''
            if atype=='land':
                r['land_area_median']=int(round(g['area'].median())) if g['area'].notna().any() else ''
                ish=g['isshu'].dropna()
                r['land_isshu_median']=int(round(ish.median())) if len(ish) else ''
                r['land_isshu_mean']=int(round(ish.mean())) if len(ish) else ''
            elif atype=='house':
                r['house_floor_median']=int(round(g['floor'].median())) if g['floor'].notna().any() else ''
                r['house_land_median']=int(round(g['area'].median())) if g['area'].notna().any() else ''
                r['house_built_median']=int(round(g['built'].median())) if g['built'].notna().any() else ''
            else:
                r['mansion_area_median']=int(round(g['area'].median())) if g['area'].notna().any() else ''
                r['mansion_area_mean']=int(round(g['area'].mean())) if g['area'].notna().any() else ''
                r['mansion_built_median']=int(round(g['built'].median())) if g['built'].notna().any() else ''
            r[f'{pre}_examples']=_examples(g, atype)
        return len(w)

    n=[agg(d1,'land'), agg(d1,'house'), agg(d2,'mansion')]
    out=pd.DataFrame(rows.values())
    out['price_data_period']=period_label; out['price_data_source']=source_label; out['price_updated']=updated
    order=(['station']+[c for c in out.columns if c.startswith('land_')]
           +[c for c in out.columns if c.startswith('house_')]
           +[c for c in out.columns if c.startswith('mansion_')]
           +['price_data_period','price_data_source','price_updated'])
    out=out.reindex(columns=order).sort_values('station').reset_index(drop=True)
    out=out.where(pd.notna(out),'')
    def fmt(v):
        if v=='' or (isinstance(v,float) and pd.isna(v)): return ''
        if isinstance(v,float) and float(v).is_integer(): return str(int(v))
        return v
    out=out.map(fmt)
    out.to_csv(out_path, index=False, encoding='utf-8')
    return {'land':n[0],'house':n[1],'mansion':n[2],'stations':len(out),'cols':out.shape[1]}
