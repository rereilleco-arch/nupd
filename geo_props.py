#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REIT物件に座標を与え、駅からの実距離を計算する。

なぜ必要か
  aggregate_by_station.py の並び順は「区」が最小単位だった。
  溜池山王駅は駅マスタの所在地が千代田区永田町2-11-1 なので自区=千代田区となり、
  千代田区の物件(神田・秋葉原・神保町)が距離0km扱いで港区(赤坂駅経由0.28km)より
  上に来ていた。「溜池山王駅周辺の賃料」として神田の賃料が中央値を作っており、
  実測で21%低く出ていた。区という粒度そのものが原因なので、実距離に置き換える。

座標の求め方(精度の高い順)
  0) 番地まで住所がある     → その番地の位置         精度 '番地'
     公式が座標を持つ法人(野村MF)はその座標、ほかは国土地理院の住所検索で出す。
     出した座標は reit_geocode.csv に保存し、次回からは新しい住所だけを問い合わせる。
     丁目の重心との差は中央値85m・最大375m、公式座標との差は中央値13m(2026-10 実測)。
  1) 丁目まで住所がある     → 町丁目の重心          精度 '丁目'
  2) 町名まで住所がある     → その町(全丁目)の重心   精度 '町名'
  3) 区しか分からない       → 物件名に含まれる同区の町名/駅名の座標  精度 '物件名'
  4) どれも不可            → 座標なし(表示しない)

  有報の所在地は7割が「東京都港区」までしか書かれていない。一方で住宅REITの
  物件名はほぼ例外なく地名を含む(レジディア赤坂/カスタリア麻布十番/
  パークアクシス板橋本町)。従来この一致は「並び順の加点」に使われていたが、
  座標を引く手がかりに使うほうが同じ情報をはるかに正確に使える。

  3) は推論なので歯止めを2つ置く。
    - 照合先はその物件の区の中に限る(区が分からない物件には座標を与えない)
    - 駅名照合は区の重心から2.5km以内の駅だけ採用する
      (「板橋区の物件名に板橋」と「北区の板橋駅」を混同しないため)

  推定の性質上、位置の誤差は町丁目重心で±200m、物件名推定で±400m程度ある。
  表示側で 0.1km 刻みに丸め「約」を付けること。丁目単位の精密な距離ではない。

依存データ(いずれも既存・恒久)
  choume.json         マンション名寄せ/_pipeline/  町丁目 -> [lat, lon, 区, 町丁目]
  station_coords.csv  NOITAS基本データ/csv/        station, lon, lat
"""
import csv
import json
import math
import os
import re

# choume.json / station_coords.csv の探索先。実行場所が
# ローカル(py/)でもデータリポジトリ(input/)でも動くように候補を並べる。
CHOUME_CANDIDATES = [
    'choume.json',
    'input/choume.json',
    '../マンション名寄せ/_pipeline/choume.json',
    os.path.expanduser('~/NOITAS/4-データ/マンション名寄せ/_pipeline/choume.json'),
]
COORDS_CANDIDATES = [
    'station_coords.csv',
    'input/station_coords.csv',
    'csv/station_coords.csv',
    os.path.expanduser('~/NOITAS/4-データ/NOITAS基本データ/csv/station_coords.csv'),
]

ZEN = str.maketrans('０１２３４５６７８９', '0123456789')
KAN = {1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '七', 8: '八',
       9: '九', 10: '十', 11: '十一', 12: '十二', 13: '十三', 14: '十四', 15: '十五'}
CHOUME_SUFFIX = re.compile(r'[一二三四五六七八九十〇]+丁目$')


def _first(paths, label):
    for p in paths:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f'{label} が見つかりません。探した場所: ' + ' / '.join(paths))


BANCHI = re.compile(r'[0-9０-９]+\s*(番|号|[-－‐ー])')   # 番地まである住所か


def addr_key(a):
    """住所の照合キー。空白と全角半角の違いを潰す"""
    import unicodedata
    return re.sub(r'[\s\u3000]+', '', unicodedata.normalize('NFKC', str(a or '')))


class Geocoder:
    """番地まである住所 -> (lat, lon)。

    保存ファイル(reit_geocode.csv: address,lat,lon,level,source)を先に引き、無ければ
    国土地理院の住所検索に問い合わせて保存する。住所は変わらないので1度引けば使い回せる。
    問い合わせに失敗したら None を返す（呼び出し側は従来の丁目重心に戻る）。"""
    URL = 'https://msearch.gsi.go.jp/address-search/AddressSearch?q='
    COLS = ['address', 'lat', 'lon', 'level', 'source']

    def __init__(self, path='reit_geocode.csv', network=True, max_new=3000):
        self.path, self.network, self.max_new = path, network, max_new
        self.rows, self.new, self.fail = {}, 0, 0
        if path and os.path.exists(path):
            with open(path, encoding='utf-8-sig') as f:
                for r in csv.DictReader(f):
                    self.rows[addr_key(r['address'])] = r

    def put(self, address, lat, lon, level, source):
        """公式の座標など、問い合わせずに分かっている位置を入れる（国土地理院より優先）"""
        self.rows[addr_key(address)] = {'address': address, 'lat': str(lat), 'lon': str(lon),
                                        'level': level, 'source': source}

    def lookup(self, address):
        k = addr_key(address)
        r = self.rows.get(k)
        if r:
            return (float(r['lat']), float(r['lon'])) if r.get('lat') else None
        if not self.network or self.new >= self.max_new or self.fail >= 20:
            return None
        import json, time, urllib.parse, urllib.request
        try:
            q = re.split(r'[（(、,]', k)[0]
            req = urllib.request.Request(self.URL + urllib.parse.quote(q),
                                         headers={'User-Agent': 'noitas data pipeline'})
            with urllib.request.urlopen(req, timeout=20) as res:
                js = json.loads(res.read().decode('utf-8') or '[]')
            time.sleep(0.25)                      # 相手に負担をかけない間隔
        except Exception:
            self.fail += 1
            return None
        self.new += 1
        if not js:
            self.rows[k] = {'address': address, 'lat': '', 'lon': '', 'level': '見つからない', 'source': '国土地理院'}
            return None
        x, y = js[0]['geometry']['coordinates']
        self.rows[k] = {'address': address, 'lat': f'{y:.6f}', 'lon': f'{x:.6f}',
                        'level': js[0]['properties'].get('title', ''), 'source': '国土地理院'}
        return (y, x)

    def save(self):
        if not self.path:
            return
        with open(self.path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=self.COLS)
            w.writeheader()
            for r in sorted(self.rows.values(), key=lambda r: r['address']):
                w.writerow({c: r.get(c, '') for c in self.COLS})


def norm_ke(s):
    """ケ/ヶ/が/ガ を正規化(市ケ谷=市ヶ谷)。aggregate_by_station と同じ規約。"""
    s = str(s or '')
    for a in ('ヶ', 'が', 'ガ', 'ケ'):
        s = s.replace(a, 'ケ')
    return s


def haversine(a, b):
    """(lat, lon) 2点間の距離km。"""
    R = 6371.0
    dla = math.radians(b[0] - a[0])
    dlo = math.radians(b[1] - a[1])
    x = (math.sin(dla / 2) ** 2
         + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlo / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def _centroid(pts):
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


class Geo:
    """町丁目座標と駅座標を持ち、物件1件ずつに座標を割り当てる。"""

    # 物件名から町名/駅名を拾うときに、区重心からこれ以上離れた候補は採らない
    WARD_GUARD_KM = 2.5

    def __init__(self, choume_path=None, coords_path=None, extract_muni=None, geocoder=None):
        self.geocoder = geocoder   # 番地まである住所の位置（無ければ従来どおり丁目重心から）
        cp = choume_path or _first(CHOUME_CANDIDATES, 'choume.json')
        sp = coords_path or _first(COORDS_CANDIDATES, 'station_coords.csv')
        # extract_muni は呼び出し側(aggregate_by_station)の実装を借りる。
        # 「大阪市港区」を東京の港区と誤判定しない処理が既にそこにあるため。
        self.extract_muni = extract_muni or (lambda _loc: None)

        raw = json.load(open(cp, encoding='utf-8'))
        self.cho = {k: v for k, v in raw.items()
                    if v and v[0] is not None and v[1] is not None}

        towns, ward_pts = {}, {}
        for v in self.cho.values():
            ward, town = v[2], v[3]
            ward_pts.setdefault(ward, []).append((v[0], v[1]))
            base = CHOUME_SUFFIX.sub('', town)
            if len(base) >= 2:
                towns.setdefault(ward, {}).setdefault(base, []).append((v[0], v[1]))
        self.town_c = {w: {t: _centroid(p) for t, p in d.items()} for w, d in towns.items()}
        self.ward_c = {w: _centroid(p) for w, p in ward_pts.items()}

        self.stations = {}
        for r in csv.DictReader(open(sp, encoding='utf-8-sig')):
            try:
                self.stations[r['station']] = (float(r['lat']), float(r['lon']))
            except (TypeError, ValueError):
                continue
        # 「駅」を落とした駅名 -> 座標。物件名照合用。
        self.st_name = {norm_ke(re.sub(r'駅$', '', s)): c for s, c in self.stations.items()}

    # ---- 住所のパース -------------------------------------------------

    def _choume_keys(self, loc):
        """choume.json のキー候補(丁目まで)を返す。全角/半角の丁目表記を吸収する。"""
        s = re.sub(r'[\s　]', '', str(loc or ''))
        if not s:
            return []
        if not s.startswith('東京都'):
            s = '東京都' + s
        m = re.match(r'(東京都[^区市町村]{1,6}?[区市町村])(.*)$', s)
        if not m:
            return []
        ward, rest = m.group(1), m.group(2)
        out = []
        mm = re.match(r'([^\d０-９]+?[一二三四五六七八九十〇]+丁目)', rest)
        if mm:
            out.append(ward + mm.group(1))
        mn = re.match(r'([^\d０-９]+?)([0-9０-９]+)', rest)
        if mn:
            n = int(mn.group(2).translate(ZEN))
            if n in KAN:
                out.append(ward + mn.group(1) + KAN[n] + '丁目')
        return out

    @staticmethod
    def _loc_town(loc):
        """住所の町名部分(丁目・番地を落とす)。'東京都港区虎ノ門3-1' -> '虎ノ門'"""
        s = re.sub(r'[\s　]', '', str(loc or ''))
        s = re.sub(r'^東京都', '', s)
        s = re.sub(r'^.*?[区市町村]', '', s)
        s = re.sub(r'[0-9０-９].*$', '', s)
        s = CHOUME_SUFFIX.sub('', s)
        return re.sub(r'丁目.*$', '', s).strip()

    # ---- 座標づけ -----------------------------------------------------

    def locate(self, loc, prop_name):
        """(lat, lon, 精度) を返す。座標が付かないときは (None, None, 'なし')。"""
        if self.geocoder and BANCHI.search(str(loc or '')):
            p = self.geocoder.lookup(loc)
            if p:
                return p[0], p[1], '番地'
        for k in self._choume_keys(loc):
            if k in self.cho:
                return self.cho[k][0], self.cho[k][1], '丁目'
        ward = self.extract_muni(loc)
        if not ward:
            return None, None, 'なし'
        t = self._loc_town(loc)
        if len(t) >= 2 and t in self.town_c.get(ward, {}):
            c = self.town_c[ward][t]
            return c[0], c[1], '町名'
        # 物件名からの推定。長い一致を優先する(「麻布十番」>「麻布」)。
        # 末尾の Ⅱ/2 は棟番号なので落とす。
        nm = norm_ke(re.sub(r'[ⅠⅡⅢⅣⅤ0-9]+$', '', str(prop_name or '')))
        best = None
        for t2, c in self.town_c.get(ward, {}).items():
            if norm_ke(t2) in nm and (best is None or len(t2) > best[0]):
                best = (len(t2), c)
        wc = self.ward_c.get(ward)
        if wc:
            for s2, c in self.st_name.items():
                if len(s2) < 2 or s2 not in nm:
                    continue
                if haversine(wc, c) > self.WARD_GUARD_KM:
                    continue        # 同名の別区の駅を拾わないための歯止め
                if best is None or len(s2) > best[0]:
                    best = (len(s2), c)
        if best:
            return best[1][0], best[1][1], '物件名'
        return None, None, 'なし'

    def station_point(self, station_title):
        return self.stations.get(station_title)
