# -*- coding: utf-8 -*-
"""種データ(マンション名) → 町丁目座標 → 駅 の紐付け。
無償ソースのみ: Geolonia japanese-addresses(町丁目座標) / OSM(駅座標)。"""
import json, csv, re, unicodedata, math, os
HERE=os.path.dirname(os.path.abspath(__file__))

KAN='〇一二三四五六七八九'
def kan2num(t):
    if t.isdigit(): return int(t)
    if '十' in t:
        a,_,b = t.partition('十')
        return (KAN.index(a) if a else 1)*10 + (KAN.index(b) if b else 0)
    return KAN.index(t) if t in KAN else 0

def norm_town(s):
    """町名を『◯◯3丁目』形式に正規化"""
    s = unicodedata.normalize('NFKC', s or '').replace(' ', '').replace('　','')
    s = re.sub(r'([0-9]+|[〇一二三四五六七八九十]+)丁目', lambda m: f'{kan2num(m.group(1))}丁目', s)
    return s

def split_addr(a):
    """所在地 → (市区町村, 町名+丁目)。番地以降は捨てる"""
    a = norm_town(a).replace('東京都','')
    m = re.match(r'^(.+?[区市町村])(.*)$', a)
    if not m: return None, None
    city, rest = m.group(1), m.group(2)
    m2 = re.match(r'^(.+?\d+丁目)', rest)
    if m2: return city, m2.group(1)
    m3 = re.match(r'^([^\d]+)', rest)      # 丁目なし町名
    return city, (m3.group(1) if m3 else rest)

def hav(a, b):
    R=6371000.0
    p1,p2=math.radians(a[0]),math.radians(b[0])
    dp=p2-p1; dl=math.radians(b[1]-a[1])
    h=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))

choume={}
for k,(lat,lng,city,town) in json.load(open(os.path.join(HERE,'choume.json'))).items():
    choume[(city, norm_town(town))]=(lat,lng)
stc={k:(v[0],v[1]) for k,v in json.load(open(os.path.join(HERE,'stcoord.json'))).items()}

# 相場を語れる駅（n>=10）だけを対象にする
N=os.path.expanduser('~/NOITAS/4-データ/')
target={}
for r in csv.DictReader(open(N+'NOITAS基本データ/csv/station_prices.csv')):
    c=r['mansion_count'].strip()
    if c.isdigit() and int(c)>=10:
        target[r['station'].replace('駅','')]=int(c)

WALK=80.0   # m/分
LIMIT=1200  # 徒歩15分

def geocode(addr):
    city, town = split_addr(addr)
    if not city: return None,None,None
    if (city,town) in choume: return choume[(city,town)], city, town
    if town:
        base=re.sub(r'\d+丁目$','',town)
        cand=[v for (c,t),v in choume.items() if c==city and t.startswith(base)]
        if cand: return cand[0], city, base
    return None, city, town

def run(src, path, name_col, addr_col, extra):
    rows=list(csv.DictReader(open(path, encoding='utf-8-sig')))
    out=[]; ng=0
    for r in rows:
        addr=r.get(addr_col,'')
        pt, city, town = geocode(addr)
        if not pt: ng+=1; continue
        near=[]
        for sn,sp in stc.items():
            if sn not in target: continue
            d=hav(pt,sp)
            if d<=LIMIT: near.append((d,sn))
        near.sort()
        if not near: ng+=1; continue
        rec={'名称':r[name_col],'所在地':addr,'区市町村':city,
             'lat':round(pt[0],6),'lng':round(pt[1],6),
             '最寄駅':near[0][1],'徒歩分':max(1,round(near[0][0]/WALK)),
             '徒歩15分圏の駅':'|'.join(f'{s}:{max(1,round(d/WALK))}' for d,s in near[:5]),
             '出典':src}
        for k,v in extra.items(): rec[k]=r.get(v,'')
        out.append(rec)
    return out, len(rows), ng

M=N+'マンション名寄せ/'
res=[]
a,t1,n1=run('建築物環境計画書', M+'seed_kankyokeikakusho.csv','名称','所在地',
            {'用途':'用途','延べ面積㎡':'延べ面積㎡','地上階':'地上階','構造':'構造','竣工年月':'竣工年月','建築主':'建築主'})
b,t2,n2=run('とどまるマンション', M+'seed_todomaru.csv','名称','所在地', {'防災星':'防災星'})
res=a+b
b2,t3,n3=run('中央区防災認定', M+'seed_chuo_bousai.csv','名称','所在地', {})
res=a+b+b2
print(f'中央区防災認定:   {len(b2)}/{t3} 紐付け成功（除外{n3}）')
print(f'建築物環境計画書: {len(a)}/{t1} 紐付け成功（除外{n1}）')
print(f'とどまる:         {len(b)}/{t2} 紐付け成功（除外{n2}）')

cols=['名称','所在地','区市町村','lat','lng','最寄駅','徒歩分','徒歩15分圏の駅','出典','用途','延べ面積㎡','地上階','構造','竣工年月','建築主','防災星']
with open(M+'mansion_station_link.csv','w',newline='',encoding='utf-8-sig') as f:
    w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore'); w.writeheader()
    for r in res: w.writerow(r)
print('→ mansion_station_link.csv:', len(res),'棟')
