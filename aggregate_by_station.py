#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
駅ごとの周辺REIT事例を生成する(近接並び替え)
==========================================
reit_properties.csv(住宅フィルタ後) と 駅マスタ(tokyost) から、
各駅について「同区の住宅REIT物件を、駅の町名に近い順に並べた上位N件」を
事前計算し、reit_by_station.csv を出力する。

並び順(案B+C):
  1) 駅の町名と物件の町名が一致 → 最優先(近接)
  2) 町名部分一致 → 次
  3) 同区の残り → 駅名をシードに決定的シャッフル(駅ごとに違う順、再現性あり)
これにより「駅ごとに表示が変わる(SEO重複回避)」かつ
「意味のある範囲では近い物件が上に来る」を両立する。

出力列:
  station              駅名(post_title。CSV取込のキー)
  reit_cap_median      同区の住宅REIT cap rate中央値(駅間で同じ=区の水準)
  reit_count           同区の住宅REIT物件数
  reit_examples        近接順に並べた事例JSON(上位N件, 日本語キー)
  rent_examples        賃料ブロック用。駅からの実距離順に並べた事例JSON(上位10件)
  rent_count           rent_examples の母数(採用半径の中に入った件数)

賃料ブロックだけ「実距離順」にしている理由
  上の並び順(1〜3)は最小単位が「区」で、駅の所在地が属する区を距離0として扱う。
  溜池山王駅は駅マスタの所在地が千代田区永田町2-11-1 なので自区=千代田区となり、
  神田・秋葉原の物件が港区(赤坂駅経由0.28km)より上に来ていた。「溜池山王駅周辺の
  賃料」の中央値が実測で21%低く出ており、賃料の指標としては成立していない。

  賃料ブロック(rent_examples)は geo_props で物件に座標を与え、駅座標からの
  実距離で並べ替える。駅ページのREIT表(reit_examples)は従来のまま触っていない。
  片方だけ差し替えて比較できる状態を残すため。

  距離はサイトに表示しない。座標は町丁目の重心か駅の位置であって物件そのものの
  位置ではないため、「約0.7km」と書くと持っていない精度を表示することになる。
  並べ替えと半径の判定にだけ使う内部値である。
"""
import csv
import json
import re
import hashlib
import statistics
import argparse

import geo_props

# 賃料ブロックの採用半径。1.5kmで足りなければ3.0kmまで段階的に広げる。
# 郊外はREIT物件そのものが少なく、半径を固定すると自駅の1件だけになるか
# ブロックが消える。読者は「駅として近いか」しか見ていないので、
# 隣の駅の物件でも入っているほうが賃料の目安として役に立つ。
# 「3.0km以内から、駅に近い順にN件」。件数を固定して半径を可変にする方式。
#
# 物件密度は駅によって極端に違う(1.5km圏に68件の岩本町と、3km圏に1件の保谷)。
# 半径を固定して中に入った全件を使うと、都心では隣の駅まで巻き込んで薄まる。
# 岩本町の1.5km圏66件は日本橋・東神田・浅草橋で、岩本町駅の賃料ではない。
# 駅の賃料を表すのは「駅に最も近い物件」なので、件数を固定するほうが実態に近い。
#
# N=10 にした根拠: 10番目の物件までの距離の中央値が1.35km。当初想定していた
# 1.5km圏にほぼ一致する。N=15 にすると1.63kmに伸び、溜池山王では1.92kmまで
# 広がって麻布台・二番町が入り、中央値が6.2%下がる(赤坂・虎ノ門・愛宕・六本木で
# 構成される1.5km圏のほうが素直)。密集駅では N を増やしても距離は伸びないが、
# 中密度の駅で効いてしまう。
#
# 母数は持たない。以前は「周辺12件のうち近い順に10件を表示」と出していたが、
# 母数を基準に中央値を計算していると誤読される。表示行=中央値の母集団=N件。
RENT_RADIUS = 3.0
RENT_MAX_SHOW = 10

# REITページ用。賃料ブロックと同じ距離計算で、3km以内・近い順30件。
#
# 以前のREITページは reit_examples（区と近隣駅の区の物件を、物件名一致→区→ランダムで
# 並べた最大223件）をそのまま出しており、溜池山王に神田が並ぶ誤りが残っていた。
# 件数を賃料(10件)と変えるのは、答える問いが違うため。賃料は場所で大きく変わるので
# 近い10件に絞るが、REITページは「この辺りの収益物件がどう取得・評価されているか」を
# 比較事例で見せるページで、NOI利回りは東京全体で3.3〜3.6%に集まり範囲を広げても
# 水準がぶれない（近い10件と30件の中央値の差は中央値0.05pt）。
# 中央値は表示する30件で取る。一覧と母数を一致させる。
# 賃料保証型のMLは外さない。NOIはREITが実際に受け取る収入なので保証型でも正しい。
REIT_NEAR_MAX = 30

# 1棟の簡易査定の事例に足す住宅REIT。駅から2km以内・近い順に最大 REIT_BLDG_MAX 件。
# 2kmは1棟の成約事例を近隣駅から集める範囲と同じ。検証（共同住宅1棟の成約891件を当てる）で
# ±20%以内が 39.6%→42.5%、±30%以内が 54.4%→60.5%、査定できる駅が 375→470 に増えた。
# 築年が無い物件は入れない（築年で補正できないと古い建物ほど高く出る。1989年以前築で1.58倍）。
REIT_BLDG_RADIUS = 2.0
REIT_BLDG_MAX = 30

TOKYO23 = ['千代田区','中央区','港区','新宿区','文京区','台東区','墨田区','江東区',
           '品川区','目黒区','大田区','世田谷区','渋谷区','中野区','杉並区','豊島区',
           '北区','荒川区','板橋区','練馬区','足立区','葛飾区','江戸川区']

# 住宅判定(aggregate_by_muni.py と同一ロジック)
RESI_USE = re.compile(r'居住|住宅|レジデン|共同住宅')
NONRESI_USE = re.compile(r'オフィス|事務所|商業|物流|ホテル|宿泊|倉庫|店舗|事業所|底地|その他')
NONRESI_NAME = re.compile(r'Dプロジェクト|ロジ|物流|ロジスティ|DPL|プロロジス|GLP|オフィス|'
                          r'センタービル|モール|アウトレット|ショッピング|ホテル')
RESI_NAME = re.compile(r'レジデン|レジデンス|レジディア|ハイツ|コーポ|メゾン|コンフォリア|プラウド|'
                       r'パークアクシス|アクシス|カーサ|ヴィラ|ガーデンホームズ|'
                       r'S-FORT|S-RESIDENCE|プロシード|アルティザ|ラグゼナ|カスタリア')
# 「アドバンス・レジデンス投資法人」は法人名が『レジデンス』で、旧版の
# 『レジデンシャル』に一致せず、住宅特化型なのに288件全てが非住宅と判定されていた。
RESI_REIT = re.compile(r'レジデンシャル|レジデンス|アコモデーション|コンフォリア|リビング|'
                       r'プロシード|サムティ・レジデン')


def is_residential(r):
    use = (r.get('use_type', '') or '').strip()
    name = r.get('property_name', '') or ''
    reit = r.get('reit_name', '') or ''
    if use:
        if RESI_USE.search(use):
            return True
        if NONRESI_USE.search(use):
            return False
    if NONRESI_NAME.search(name):
        return False
    if RESI_NAME.search(name):
        return True
    if RESI_REIT.search(reit):
        return True
    return False


# 東京以外の道府県・政令市。「大阪市港区」「神戸市中央区」「横浜市港北区」等が
# 東京の同名区として誤判定されるのを防ぐ(実データで154件の誤判定を確認)。
OTHER_PREF_RX = re.compile(
    r'(北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|埼玉県|千葉県|'
    r'神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|岐阜県|静岡県|愛知県|三重県|滋賀県|'
    r'京都府|大阪府|兵庫県|奈良県|和歌山県|鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|'
    r'愛媛県|高知県|福岡県|佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県)')
OTHER_CITY_RX = re.compile(
    r'(札幌|仙台|さいたま|千葉|横浜|川崎|相模原|新潟|静岡|浜松|名古屋|京都|大阪|堺|神戸|'
    r'岡山|広島|北九州|福岡|熊本)市')


def extract_muni(loc):
    """所在地から東京の市区町村を返す。東京以外は None。

    「大阪市港区」「神戸市中央区」「横浜市港北区」のように、他都市にも
    東京と同名の区が存在する。単純な部分一致だと東京の物件として扱ってしまい、
    駅ページに他県の物件が混ざるため、都道府県・政令市を先に判定して弾く。
    """
    s = str(loc or '')
    if not s:
        return None
    if '東京都' in s:
        s = s.split('東京都', 1)[1]          # 「東京都」以降だけを見る
    else:
        # 東京都と明記が無い場合、他の道府県・政令市が出てきたら東京ではない
        if OTHER_PREF_RX.search(s) or OTHER_CITY_RX.search(s):
            return None
    for w in TOKYO23:
        if w in s:
            return w
    m = re.search(r'^([^\s]+?市)', s.strip())
    return m.group(1) if m else None


def extract_town(addr):
    """住所から町名(丁目・番地の手前)を抽出。先頭が漢数字の町名(五番町・九段南)にも対応。"""
    if not addr:
        return ''
    s = str(addr)
    s = re.sub(r'^.*?[区市町村]', '', s)          # 区市町村まで除去
    if not s:
        return ''
    # 「丁目」「番」「-」「数字＋丁目」の手前までを町名とする。
    # 先頭の漢数字は町名の一部として残す(五番町/六番町/四番町/九段南)。
    m = re.match(r'(.+?)(?:[一二三四五六七八九十〇\d０-９]+丁目|[０-９\d]|番|$)', s)
    town = m.group(1).strip() if m else s
    # 末尾に残った半端な漢数字境界を除かない(「九段南」等を保つ)
    return town


def norm_ke(s):
    """ケ/ヶ/が/ガ を正規化(市ケ谷=市ヶ谷)"""
    s = str(s)
    for a in ('ヶ', 'が', 'ガ', 'ケ'):
        s = s.replace(a, 'ケ')
    return s


def station_keywords(station_name, addr):
    """駅名と駅住所から、物件名照合用のキーワード集合を作る。
    例: 市ケ谷駅/千代田区五番町 -> {'市ケ谷','五番町'}
        秋葉原駅/千代田区外神田 -> {'秋葉原','外神田'}
    物件名にこれらの語が含まれれば「その駅の近く」とみなす。"""
    kws = set()
    base = re.sub(r'駅$', '', station_name)
    if len(base) >= 2:
        kws.add(norm_ke(base))
    town = extract_town(addr)
    if len(town) >= 2:
        kws.add(norm_ke(town))
    return {k for k in kws if len(k) >= 2}


def name_proximity_rank(keywords, prop_name):
    """物件名に駅キーワードが含まれれば0(近接)、なければ2。"""
    pn = norm_ke(prop_name or '')
    for k in keywords:
        if k in pn:
            return 0
    return 2


def norm_station(s):
    """駅名正規化(自駅重複・表記揺れの同一視用)"""
    s = re.sub(r'[\s\u3000]+', '', str(s or ''))
    s = re.sub(r'駅$', '', s)
    for a in ('ヶ', 'が', 'ガ', 'ケ'):
        s = s.replace(a, 'ケ')
    return s


def load_neighbors(path):
    """近隣駅CSV(station,neighbors,neighbor_dists)を読む。
    返り値: {駅名: [(近隣駅名, 距離km), ...]} 自駅と同名(表記揺れ)は除外。"""
    out = {}
    try:
        with open(path, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                st = r['station']
                ns = (r.get('neighbors') or '').split('|')
                ds = (r.get('neighbor_dists') or '').split('|')
                pairs = []
                self_key = norm_station(st)
                for n, d in zip(ns, ds):
                    if not n or norm_station(n) == self_key:
                        continue   # 自駅の表記揺れ重複は除外
                    try:
                        pairs.append((n, float(d)))
                    except ValueError:
                        pairs.append((n, 999.0))
                out[st] = pairs
    except FileNotFoundError:
        pass
    return out


def _norm_pname(s):
    """物件名の表記ゆれを吸収するキー。スペース除去＋英数字を半角化。
    有報とREIT公式で「みなとみらい オーシャンタワー / みなとみらいオーシャンタワー」
    「早稲田ＤＥＵＸ / 早稲田DEUX」のように空白と全半角が食い違うため。"""
    import unicodedata
    s = unicodedata.normalize('NFKC', str(s or ''))
    return re.sub(r'[\s\u3000]+', '', s)


def _official_key(name):
    """公式サイトと有報の物件名の照合キー。

    空白・全角半角に加え、積水ハウス・リートは「Ⅱ」を小文字のエル2つ「ll」で書くので
    （エスティメゾン恵比寿ll）、末尾の ll/LL を II にそろえる。"""
    s = _norm_pname(name).upper()
    return re.sub(r'LL$', 'II', s)


def load_location_master(path):
    """reit_locations.csv(公式サイト由来の恒久データ)を読む。

    有報の物件表に住所を載せない法人があり(ARI・コンフォリア・大和証券リビング・
    KDX・野村MF 等)、そのままでは extract_muni() が None を返して駅ページから
    消える。公式サイトの所在地で補完する。有報の location が有る物件には触らない。
    """
    out = {}
    try:
        with open(path, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                loc = r['location']
                out[(r['reit_name'], r['property_name'])] = loc
                # 表記ゆれ吸収用の別キーも張る(完全一致を優先するため後で引く)
                out.setdefault((r['reit_name'], _norm_pname(r['property_name']) + '\x00norm'), loc)
    except FileNotFoundError:
        pass
    return out


def load_detail_master(path):
    """reit_detail_scan.py の出力(個別物件明細の住所・ML種別)を読む。

    キーは load_location_master と同じ規約:
      (法人名, 物件名) と (法人名, 正規化物件名 + '\\x00norm') の2本を張る。
    ファイルが無ければ空を返す(この補完は任意)。
    """
    out = {}
    try:
        with open(path, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                v = {'address': r.get('address', ''), 'ml_class': r.get('ml_class', ''),
                     'granularity': r.get('granularity', ''), 'built': r.get('built', '')}
                out[(r['reit_name'], r['property_name'])] = v
                out.setdefault((r['reit_name'], _norm_pname(r['property_name']) + '\x00norm'), v)
    except FileNotFoundError:
        pass
    return out


def detail_rank(addr):
    """住所の粒度を数値化する。大きいほど細かい。上書きの可否判定に使う。"""
    s = str(addr or '')
    if not s:
        return 0
    if re.search(r'[一二三四五六七八九十〇\d０-９]+丁目', s):
        return 3
    if re.search(r'[0-9０-９]', s):
        return 2
    # 「東京都渋谷区」だけなら町名が無い
    body = re.sub(r'^東京都', '', s)
    body = re.sub(r'^.*?[区市町村]', '', body)
    return 2 if len(body.strip()) >= 2 else 1


def to_float(s):
    try:
        return float(s) if s not in (None, '') else None
    except ValueError:
        return None


TSUBO = 3.305785

def period_end_year(period):
    """有報の期間表記から決算期末の年を取り出す。

    例: '有価証券報告書（内国投資証券）－第41期(2025/11/01－2026/04/30)' -> 2026
    括弧内の日付は「開始－終了」なので、最後に現れる年を期末とみなす。
    読めなければ None(表示側で時点を書かない)。
    """
    if not period:
        return None
    ys = re.findall(r'(20\d{2})[/年]', str(period))
    return int(ys[-1]) if ys else None


def rent_per_tsubo(p):
    """入居中住戸の月額賃料から 円/坪・月 を出す。

    分母は「賃貸可能面積 × 稼働率」= 実際に賃貸されている面積。
    有報の「月額賃料」は満室想定ではなく、締結済みの賃貸借契約の合計額である。
    主要12法人の注記を実査したところ、8法人が「締結されている賃貸借契約書等に
    表示された月額賃料」と明示し、満室想定と書く法人は1社も無かった
    (有報は現況を開示する書類なので想定値を載せない)。

    賃貸可能面積で割ると空室分だけ坪単価が低く出る。稼働97.9%なら誤差2%だが、
    稼働84.6%の物件では18%低く見え、「賃料が高いが空室が多い物件」を
    「賃料が安い物件」と取り違える。VUのベンチマークとしては致命的。

    稼働率が無い物件は満室(100%)とみなさず、そのまま賃貸可能面積で割る。
    過大に出すより控えめに出すほうが安全なため(実勢の下限として読める)。

    rent_monthly_mn は reit_parser が有報の収入列から作る月額(百万円)。
    年間表記の法人は12で割って揃えてあり、期間が読めない列は空にしてある
    (三井不動産アコモデーションの「当期中に受け取った賃貸事業収入」など)。
    """
    m = to_float(p.get('rent_monthly_mn'))
    a = to_float(p.get('leasable_area'))
    if not m or not a or a <= 0:
        return None
    occ = to_float(p.get('occupancy'))
    leased = a * (occ / 100.0) if occ and 0 < occ <= 100 else a
    if leased <= 0:
        return None
    return int(round(m * 1_000_000 / (leased / TSUBO)))


def build_rent_examples(station_title, station_pt, rent_pool):
    """駅に近い順に RENT_MAX_SHOW 件を返す(RENT_RADIUS より遠い物件は対象外)。

    戻り値 (examples, 件数, N件目までの距離km)。座標が無い駅や
    半径内に1件も無い駅は ([], 0, None) を返し、表示側でブロックごと出さない。
    """
    if not station_pt:
        return [], 0, None
    # 同一距離のときに駅名を含む物件を先に出すための照合キー。
    # 座標は町丁目の重心なので、同じ町の物件は距離が小数点以下まで一致する。
    # そこをハッシュだけで切ると「レジディア目黒Ⅳが目黒駅の表に出ない」「カスタリア
    # 市ヶ谷が市ケ谷駅の表に出ない」といったことが起きる(実測7件)。
    base = geo_props.norm_ke(re.sub(r'駅$', '', station_title))
    if len(base) < 2:
        base = None

    cand = [(geo_props.haversine(station_pt, p['pt']), p) for p in rent_pool]
    d = sorted(
        (x for x in cand if x[0] <= RENT_RADIUS),
        # 同一距離は 精度 → 駅名一致 → 物件名ハッシュ で決める。距離順は崩さない。
        # 賃料の高い順に並べると「高い物件だけ選んだ」ことになるので使わない。
        key=lambda x: (x[0], x[1]['acc_rank'],
                       0 if (base and base in geo_props.norm_ke(x[1]['name'])) else 1,
                       stable_shuffle_key(station_title, x[1]['name'])))
    if not d:
        return [], 0, None
    keep = d[:RENT_MAX_SHOW]
    out = []
    for _, p in keep:
        out.append({
            '物件': p['name'], 'REIT': p['reit'],
            '月坪賃料': p['rent'], '稼働率': p['occ'],
            'NOI利回り': p['cap'], '賃料時点': p['asof'],
        })
    return out, len(out), round(keep[-1][0], 2)


def build_reit_near(station_title, station_pt, reit_pool):
    """REITページ用に、3km以内の物件を近い順に最大 REIT_NEAR_MAX 件返す。
    並べ替えキーは賃料と同じ（距離 → 位置精度 → 駅名一致 → ハッシュ）。"""
    if not station_pt:
        return []
    base = geo_props.norm_ke(re.sub(r'駅$', '', station_title))
    if len(base) < 2:
        base = None
    cand = [(geo_props.haversine(station_pt, p['pt']), p) for p in reit_pool]
    d = sorted((x for x in cand if x[0] <= RENT_RADIUS),
               key=lambda x: (x[0], x[1]['acc_rank'],
                              0 if (base and base in geo_props.norm_ke(x[1]['name'])) else 1,
                              stable_shuffle_key(station_title, x[1]['name'])))
    return [p['item'] for _, p in d[:REIT_NEAR_MAX]]


def build_reit_bldg(station_title, station_pt, bldg_pool):
    """1棟の簡易査定用に、2km以内の住宅REITを近い順に最大 REIT_BLDG_MAX 件返す。
    各件に駅からの距離(km)を付ける。並べ替えキーは build_reit_near と同じ。"""
    if not station_pt:
        return []
    cand = [(geo_props.haversine(station_pt, p['pt']), p) for p in bldg_pool]
    d = sorted((x for x in cand if x[0] <= REIT_BLDG_RADIUS),
               key=lambda x: (x[0], x[1]['acc_rank'], stable_shuffle_key(station_title, x[1]['name'])))
    return [dict(p['item'], km=round(dk, 2)) for dk, p in d[:REIT_BLDG_MAX]]


def stable_shuffle_key(station_name, prop_name):
    """駅名+物件名のハッシュ。駅ごとに決定的だが物件間で擬似ランダムな順序を与える。"""
    h = hashlib.md5((station_name + '|' + prop_name).encode('utf-8')).hexdigest()
    return int(h[:8], 16)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='infile', default='reit_properties.csv')
    ap.add_argument('--stations', default='input/tokyost_1.csv')
    ap.add_argument('--neighbors', default='station_neighbors.csv',
                    help='近隣駅リスト(station,neighbors,neighbor_dists)。恒久データ。')
    ap.add_argument('--locations', default='reit_locations.csv',
                    help='REIT公式サイト由来の物件所在地(恒久データ)。'
                         '有報に住所が無い法人の補完に使う。無ければ補完しない。')
    ap.add_argument('--details', default='reit_detail_locations.csv',
                    help='有報の個別物件明細から取った住所とML種別(reit_detail_scan.py の出力)。'
                         '物件表の所在地より粒度が高いので、あれば優先する。無ければ使わない。')
    ap.add_argument('--official', default='reit_official_addresses.csv',
                    help='REIT公式サイトの物件データから取った番地までの住所と座標(fetch_official_addresses.py の出力)。'
                         '有報・明細より細かいときだけ上書きする。無ければ使わない。')
    ap.add_argument('--geocode', default='reit_geocode.csv',
                    help='番地まである住所の位置の保存ファイル。無い住所は国土地理院に問い合わせて足す。')
    ap.add_argument('--no-network', action='store_true', help='国土地理院に問い合わせない(保存済みの位置だけ使う)')
    ap.add_argument('--out', default='reit_by_station.csv')
    ap.add_argument('--limit', type=int, default=0,
                    help='駅ごとの事例保持件数(0=全件)。表示件数はプラグイン側で絞るため既定は全件。')
    args = ap.parse_args()

    # 住宅REIT物件を市区町村ごとに集める
    with open(args.infile, encoding='utf-8-sig') as f:
        all_props = list(csv.DictReader(f))
    props = [r for r in all_props if is_residential(r)]

    # 有報に住所が無い物件を公式サイト由来データで補完する。
    # 出所を location_source に残す(一次データと二次データを混ぜない)。
    locmaster = load_location_master(args.locations)
    filled_from_master = 0
    for r in props:
        if (r.get('location') or '').strip():
            r['location_source'] = '有報'
            continue
        rn = r.get('reit_name', '')
        loc = locmaster.get((rn, r.get('property_name', '')))
        if not loc:
            loc = locmaster.get((rn, _norm_pname(r.get('property_name', '')) + '\x00norm'))
        if loc:
            r['location'] = loc
            r['location_source'] = 'REIT公式'
            filled_from_master += 1
        else:
            r['location_source'] = ''

    # 有報の個別物件明細の住所で上書きする。
    #
    # 物件表の所在地は7割が「東京都渋谷区」までしか書かれていないが、個別明細には
    # 番地まで載っている。カスタリア原宿は物件表が「東京都渋谷区」なので物件名から
    # 原宿駅の座標そのもの(0.0km)を当てていたが、明細には「渋谷区千駄ケ谷三丁目
    # 55番3号」とあり、実際は原宿駅から徒歩8分。同じ一次資料なので上書きして良い。
    #
    # ML種別も同じ明細にあるので、ここで拾って持たせる。パススルー型なら有報の
    # 賃料はエンドテナントの転貸賃料、賃料保証型ならML会社がREITに払う保証賃料で
    # 意味が違う(現状は除外していない。除外するならこの列を見る)。
    details = load_detail_master(args.details)
    filled_from_detail = 0
    for r in props:
        d = (details.get((r.get('reit_name', ''), r.get('property_name', '')))
             or details.get((r.get('reit_name', ''), _norm_pname(r.get('property_name', '')) + '\x00norm')))
        r['ml_class'] = d['ml_class'] if d else ''
        # 建築時期（「2004年12月」など）の年。1棟の査定事例に使う
        m = re.search(r'((?:19|20)\d{2})年', (d or {}).get('built', '') or '')
        r['_built'] = int(m.group(1)) if m else None
        if not d:
            continue
        # 明細の住所のほうが粒度が高いときだけ上書きする
        if detail_rank(d['address']) > detail_rank(r.get('location', '')):
            r['location'] = d['address']
            r['location_source'] = '有報明細'
            filled_from_detail += 1

    # REIT公式サイトの番地までの住所で上書きする。
    #
    # 積水ハウス・リートは有報に「東京都品川区上大崎」と町名まで、KDXは区までしか書かない。
    # reit_locations.csv(公式由来)は有報の所在地が「空」のときしか使っていなかったので、
    # 町名まで書いてある法人には番地があっても効かなかった。細かいときは上書きする。
    # 公式が座標を持つ法人(野村MF)は、その座標を住所の位置として登録する。
    geocoder = geo_props.Geocoder(args.geocode, network=not args.no_network)
    official = {}
    try:
        with open(args.official, encoding='utf-8-sig') as f:
            for o in csv.DictReader(f):
                official[(o['reit_name'], _official_key(o['property_name']))] = o
                if o.get('lat') and o.get('lon'):
                    geocoder.put(o['address'], o['lat'], o['lon'], '公式座標', o.get('source', ''))
    except FileNotFoundError:
        pass
    filled_from_official = 0
    for r in props:
        o = official.get((r.get('reit_name', ''), _official_key(r.get('property_name', ''))))
        if not o:
            continue
        cur = r.get('location', '')
        if detail_rank(o['address']) > detail_rank(cur) or (
                geo_props.BANCHI.search(o['address']) and not geo_props.BANCHI.search(cur)):
            r['location'] = o['address']
            r['location_source'] = 'REIT公式(番地)'
            filled_from_official += 1
    print(f"公式サイトの番地で上書き: {filled_from_official}件")

    # 脱落の内訳を数える。住宅と判定されても location が空だと extract_muni() が
    # None を返し、その物件は駅ページに一切出ない。件数が急に減った場合に
    # パース側の劣化なのかを、このログだけで切り分けられるようにする。
    drop_no_loc = drop_other_pref = 0
    drop_by_reit = {}

    by_muni = {}
    for r in props:
        muni = extract_muni(r.get('location'))
        if not muni:
            if not (r.get('location') or '').strip():
                drop_no_loc += 1
                k = r.get('reit_name', '')
                drop_by_reit[k] = drop_by_reit.get(k, 0) + 1
            else:
                drop_other_pref += 1
            continue
        r['_town'] = extract_town(r.get('location', ''))
        r['_cap'] = to_float(r.get('cap_rate'))
        r['_app'] = to_float(r.get('appraisal_value'))
        if r['_app'] is None:
            continue   # 鑑定評価額が無い物件は事例に出さない
        by_muni.setdefault(muni, []).append(r)

    # ---- 賃料ブロック用の座標付け ----
    # 対象は「月坪賃料が出る住宅物件」だけ。鑑定評価額の有無は問わない
    # (賃料ブロックは利回り事例ではないため)。
    geo = geo_props.Geo(extract_muni=extract_muni, geocoder=geocoder)
    rent_pool, geo_acc = [], {}
    reit_pool = []   # REITページ用（cap・鑑定評価額・座標がそろう住宅物件。ML種別は問わない）
    for r in props:
        cap = to_float(r.get('cap_rate')); app = to_float(r.get('appraisal_value'))
        if cap is None or app is None:
            continue
        la, lo, acc = geo.locate(r.get('location', ''), r.get('property_name', ''))
        if la is None:
            continue
        reit_pool.append({
            'pt': (la, lo), 'acc_rank': {'番地': 0, '丁目': 1, '町名': 2, '物件名': 3}.get(acc, 4),
            'name': r.get('property_name', ''),
            'item': {
                '物件': r.get('property_name', ''), 'REIT': r.get('reit_name', ''),
                '取得百万': to_float(r.get('acquisition_price')), '鑑定百万': app,
                'NOI利回り': cap, '稼働率': to_float(r.get('occupancy')),
                '町名': extract_town(r.get('location', '')),
                '月坪賃料': rent_per_tsubo(r), '賃料時点': period_end_year(r.get('period')),
                # 賃貸可能面積。REITページで専有坪単価（鑑定評価額÷賃貸可能面積）を出すのに使う
                '賃貸m2': to_float(r.get('leasable_area')),
            },
        })

    # ---- 1棟の簡易査定に足す住宅REIT ----
    # 価格は鑑定評価額（今の価値。取得価格は取得時点の値）。面積は賃貸可能面積＝専有面積にあたる。
    # 延床面積は開示している物件だけにあり、無い物件は表示側でレンタブル比（下）から出す。
    bldg_pool = []
    rb = []   # 賃貸可能面積 ÷ 延床面積（両方を開示している物件）
    for r in props:
        app, la_, gfa = to_float(r.get('appraisal_value')), to_float(r.get('leasable_area')), to_float(r.get('gross_floor_area'))
        if la_ and gfa and 0.4 <= la_ / gfa <= 1.0:
            rb.append(la_ / gfa)
        if not app or not la_ or not r.get('_built'):
            continue
        la, lo, acc = geo.locate(r.get('location', ''), r.get('property_name', ''))
        if la is None:
            continue
        bldg_pool.append({
            'pt': (la, lo), 'acc_rank': {'番地': 0, '丁目': 1, '町名': 2, '物件名': 3}.get(acc, 4),
            'name': r.get('property_name', ''),
            # 容量を抑えるため、使う項目だけ・無い値は持たない（全駅×30件で CSV が +1.9MB）
            'item': {k: v for k, v in (('物件', r.get('property_name', '')), ('鑑定百万', app), ('賃貸m2', la_),
                                       ('延床m2', gfa), ('土地m2', to_float(r.get('land_area'))), ('築年', r['_built']),
                                       # 位置の精度。番地で測れたものは持たない（容量のため）。表示側は有れば「概算」と注記する
                                       ('位置', None if acc == '番地' else acc))
                     if v not in (None, '')},
        })
    # レンタブル比。決め打ちせず、開示している物件の中央値を毎回出す（2026-09 実測 134件で0.85）
    rentable_ratio = round(statistics.median(rb), 3) if len(rb) >= 20 else ''
    # 純収益率（純収益 ÷ 年間賃料）。1棟の収益還元（価格＝年間賃料×純収益率÷還元利回り）に使う。
    # cap_rate は鑑定評価の直接還元利回りで、NOIではなく純収益（NCF）を還元する利回り。
    # 純収益＝還元利回り×鑑定評価額、年間賃料＝有報の月額賃料×12。東京の住宅REITの中央値（2026-09 実測 688件で0.748）
    nr = []
    for r in props:
        cap, app, rm = to_float(r.get('cap_rate')), to_float(r.get('appraisal_value')), to_float(r.get('rent_monthly_mn'))
        if cap and app and rm and extract_muni(r.get('location')):
            x = cap / 100 * app / (rm * 12)
            if 0.3 < x < 1.0:
                nr.append(x)
    ncf_ratio = round(statistics.median(nr), 3) if len(nr) >= 20 else ''

    excluded_ml = []
    for r in props:
        rt = rent_per_tsubo(r)
        if rt is None:
            continue
        # 賃料保証型・固定型のマスターリース物件は外す。
        #
        # 有報の「年間賃料」は、パススルー型ならML会社がエンドテナントから受け取る
        # 転貸賃料だが、保証型ならML会社がREITに払う保証賃料である。後者は市場賃料
        # ではないので、混ぜると別の指標を平均することになる。
        #
        # 実測で保証型は12件(東京の賃料対象695件の1.7%)しかないが、578駅のうち66駅の
        # 中央値が動く。ADRが都心をパススルー、周辺部を保証型にしているため影響が
        # 板橋・北・練馬・西東京に集中し、浮間舟渡+18.8%・赤羽+11.0%と、
        # 特定エリアを系統的に安く見せていた。
        #
        # 保証型が拾っているのは「賃料が低い物件」ではなく「市場賃料でない物件」である。
        # カレッジコート田無(15,887円/坪)は学生専用レジデンスで、住戸が極小なため
        # 坪単価が高く出る。オペレーター保証なので保証型になっている。除外すると
        # 西武柳沢の中央値は下がる(-29.8%)が、それが田無の実勢である。
        if r.get('ml_class') == '保証・固定':
            excluded_ml.append((r.get('property_name', ''), rt))
            continue
        la, lo, acc = geo.locate(r.get('location', ''), r.get('property_name', ''))
        geo_acc[acc] = geo_acc.get(acc, 0) + 1
        if la is None:
            continue
        rent_pool.append({
            'pt': (la, lo),
            'acc': acc,
            # 精度の順位。同じ距離に並んだとき、住所で引けた物件を物件名推定より前に出す。
            'acc_rank': {'番地': 0, '丁目': 1, '町名': 2, '物件名': 3}.get(acc, 4),
            'rent': rt,
            'name': r.get('property_name', ''),
            'reit': r.get('reit_name', ''),
            'occ': to_float(r.get('occupancy')),
            'cap': to_float(r.get('cap_rate')),
            'asof': period_end_year(r.get('period')),
        })

    # 区ごとの中央値・件数(駅間で共通)
    muni_stat = {}
    for muni, plist in by_muni.items():
        caps = [p['_cap'] for p in plist if p['_cap'] is not None]
        muni_stat[muni] = {
            'cap_median': round(statistics.median(caps), 2) if caps else '',
            'count': len(plist),
        }

    # 駅マスタを読む(重複駅は最初の住所を採用)
    stations = {}
    with open(args.stations, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            title = (r.get('post_title') or '').strip()
            addr = (r.get('address') or '').strip()
            if title and title not in stations:
                stations[title] = addr

    # 駅 -> 区 のマップ(近隣駅の区を引くのに使う)
    station_muni = {t: extract_muni(a) for t, a in stations.items()}

    # 近隣駅リスト(恒久データ)。自駅の区に物件が少ない場合、近い駅の区の物件で補完する。
    neighbors = load_neighbors(args.neighbors)

    out_rows = []
    reach = []          # N件目までの距離。どこまで探しているかを実行ログで見る
    for title, addr in sorted(stations.items()):
        muni = station_muni.get(title)
        if not muni or muni not in by_muni:
            continue
        st_kws = station_keywords(title, addr)

        # 各区に「自駅からの距離」を割り当てる。
        #   自区 = 距離0(最も近い)
        #   近隣駅の区 = その駅までの距離
        # これで「物件名一致 > 自駅から近い順(自区含む)」で並べられる(解釈B)。
        muni_dist = {muni: 0.0}
        for nb_name, nb_dist in neighbors.get(title, []):
            nb_muni = station_muni.get(nb_name)
            if nb_muni and nb_muni in by_muni and nb_muni not in muni_dist:
                muni_dist[nb_muni] = nb_dist

        # 対象物件 = 自区 + 近隣駅の区。各物件にソートキーを付ける。
        pool = []
        for mu, mdist in muni_dist.items():
            # その区に対して、駅名キーワードは自区なら自駅、近隣区ならその近隣駅のものを使う
            if mu == muni:
                kws = st_kws
                seed = title
            else:
                # この区に対応する近隣駅(最も近いもの)の名前でキーワード/シードを作る
                nb_name = next((n for n, d in neighbors.get(title, [])
                                if station_muni.get(n) == mu), title)
                kws = station_keywords(nb_name, stations.get(nb_name, ''))
                seed = nb_name
            for p in by_muni[mu]:
                name_rank = name_proximity_rank(kws, p['property_name'])
                pool.append((name_rank, mdist, stable_shuffle_key(seed, p['property_name']), mu, p))

        # 物件名一致(rank0) > 自駅から近い区順 > 決定的シャッフル
        pool.sort(key=lambda x: (x[0], x[1], x[2]))

        # 重複物件(同一物件が複数区に出ることはないが念のため)を除去しつつ整形
        examples = []
        seen = set()
        keep_pool = pool if args.limit <= 0 else pool[:args.limit]
        for name_rank, mdist, _, mu, p in keep_pool:
            # 駅ページの事例はNOI利回り(cap)がある物件のみ。近さ順5件に「—」が
            # 混じると利回り事例として機能しない(5件中3件が—になる駅もある)ため。
            # 区の件数(reit_count)・cap中央値は全物件ベースのまま(母数は偽らない)。
            if p['_cap'] is None:
                continue
            key = (p.get('property_name', ''), p.get('reit_name', ''))
            if key in seen:
                continue
            seen.add(key)
            item = {
                '物件': p.get('property_name', ''),
                'REIT': p.get('reit_name', ''),
                '取得百万': to_float(p.get('acquisition_price')),
                '鑑定百万': p['_app'],
                'NOI利回り': p['_cap'],
                '稼働率': to_float(p.get('occupancy')),
                '町名': p['_town'],
            }
            # 月坪賃料。プロが実際に取れている賃料で、現行の賃料坪単価
            # (FUDOSAN DBの標準条件モデル推定・市区町村単位49通り)とは性格が違う。
            # 表示対象5件のうち86.4%が埋まる(実測)。埋まらない物件は '—' になるが、
            # 近接順を崩してまで賃料のある物件を優先はしない(並びの意味が壊れるため)。
            item['月坪賃料'] = rent_per_tsubo(p)
            # 賃料の時点。period は「有価証券報告書…第41期(2025/11/01－2026/04/30)」形式で、
            # 決算期は法人ごとに違う。表示側で「◯年時点」と出すために期末の年を持たせる。
            item['賃料時点'] = period_end_year(p.get('period'))
            examples.append(item)

        # 賃料ブロックは区ではなく駅座標からの実距離で選ぶ(上の examples とは別系統)
        reit_near = build_reit_near(title, geo.station_point(title), reit_pool)
        near_caps = [e['NOI利回り'] for e in reit_near if e['NOI利回り'] is not None]
        bldg_near = build_reit_bldg(title, geo.station_point(title), bldg_pool)
        rent_ex, rent_n, rent_r = build_rent_examples(
            title, geo.station_point(title), rent_pool)
        if rent_r is not None:
            reach.append(rent_r)

        out_rows.append({
            'station': title,
            'reit_cap_median': muni_stat[muni]['cap_median'],
            'reit_count': muni_stat[muni]['count'],
            # 旧一覧（区と近隣区・最大223件）は書き出さない。REITページと駅トップは
            # reit_near_*（3km以内・近い順30件）に移った。列は残して空にする（CSVが
            # 9.8MB→14.6MBに膨らみ、取込が重くなるため）。
            'reit_examples': '',
            'rent_examples': json.dumps(rent_ex, ensure_ascii=False) if rent_ex else '',
            # 0件でも数値を入れる（取込は空セルを「既存値を保持」と扱うため、空だと
            # プラグインが「未取込」と「3km以内に0件」を区別できず、区単位の旧データに落ちる）
            'rent_count': rent_n,
            'reit_near_examples': json.dumps(reit_near, ensure_ascii=False) if reit_near else '',
            'reit_near_cap_median': round(statistics.median(near_caps), 2) if near_caps else '',
            # 0件の駅も必ず数値を入れる。取込は空セルを「既存値を保持」と扱うため、
            # 空にするとプラグインが「未取込」と「3km以内に0件」を区別できない。
            'reit_near_count': len(reit_near),
            # 1棟の簡易査定の事例（2km以内の住宅REIT）。km は駅からの距離で、表示側で徒歩分にする
            'reit_bldg_examples': json.dumps(bldg_near, ensure_ascii=False) if bldg_near else '',
            'reit_rentable_ratio': rentable_ratio,
            'reit_ncf_ratio': ncf_ratio,
        })

    # 上書き前に旧版を読み、差分を出す(別途 diff を取らなくても変化に気づけるように)
    prev = {}
    try:
        with open(args.out, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                prev[r['station']] = r
    except FileNotFoundError:
        pass

    with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['station', 'reit_cap_median', 'reit_count',
                                          'reit_examples', 'rent_examples', 'rent_count',
                                          'reit_near_examples', 'reit_near_cap_median', 'reit_near_count',
                                          'reit_bldg_examples', 'reit_rentable_ratio', 'reit_ncf_ratio'])
        w.writeheader()
        w.writerows(out_rows)

    # ---- 実行サマリ ----
    print(f"駅ごと近接事例を生成: {len(out_rows)}駅 -> {args.out}")
    print(f"  住宅判定 {len(props)}件 / 全{len(all_props)}件")
    print(f"  区が引けて採用 {sum(len(v) for v in by_muni.values())}件")
    print(f"  公式サイト由来で住所を補完: {filled_from_master}件 "
          f"(マスタ {len(locmaster)}件)")
    print(f"  有報の個別物件明細で住所を上書き: {filled_from_detail}件 "
          f"(明細 {len(details)//2}件)")
    print(f"  脱落: location欄が空 {drop_no_loc}件 / 東京都外 {drop_other_pref}件")

    geocoder.save()
    print(f"\n[位置] 番地の位置 保存 {len(geocoder.rows)}件（今回 国土地理院に問い合わせ {geocoder.new}件・失敗 {geocoder.fail}件）→ {args.geocode}")
    print(f"\n[賃料ブロック] 月坪賃料が出る物件 {sum(geo_acc.values())}件 の座標づけ")
    for k in ('番地', '丁目', '町名', '物件名', 'なし'):
        if geo_acc.get(k):
            print(f"   {k:<6}{geo_acc[k]:>5}件" + ('   ← 座標なし=採用しない' if k == 'なし' else ''))
    print(f"   採用 {len(rent_pool)}件"
          + (f"（賃料保証・固定型のML物件 {len(excluded_ml)}件を除外）" if excluded_ml else ''))
    for nm, rt in sorted(excluded_ml, key=lambda x: -x[1]):
        print(f"      除外 {nm[:24]:<26}{rt:>7,}円/坪")
    if reach:
        reach.sort()
        q = lambda f: reach[min(len(reach) - 1, int(len(reach) * f))]
        print(f'   最後の1件までの距離: 中央 {reach[len(reach)//2]:.2f}km / '
              f'上位25% {q(0.75):.2f}km / 上位10% {q(0.90):.2f}km / 最遠 {reach[-1]:.2f}km')
    filled = sum(1 for r in out_rows if r['rent_examples'])
    print(f"   賃料ブロックを出せる駅 {filled}/{len(out_rows)}")
    if drop_by_reit:
        top = sorted(drop_by_reit.items(), key=lambda x: -x[1])[:5]
        print("  location欠損の多い法人: " + ', '.join(f'{k} {v}件' for k, v in top))

    if prev:
        now = {r['station']: r for r in out_rows}
        added = sorted(set(now) - set(prev))
        removed = sorted(set(prev) - set(now))
        changed = [s_ for s_ in set(now) & set(prev)
                   if now[s_].get('reit_near_examples', '') != prev[s_].get('reit_near_examples', '')
                   or now[s_].get('rent_examples', '') != prev[s_].get('rent_examples', '')]
        print(f"\n[前回との差分] 駅 追加{len(added)} / 削除{len(removed)} / 内容変化{len(changed)}")
        if removed:
            print(f"  削除された駅(要確認): {removed[:10]}")
        # 件数が大きく減った駅は劣化の疑いがあるので個別に出す
        worse = []
        for s_ in set(now) & set(prev):
            try:
                a, b = int(now[s_]['reit_near_count']), int(prev[s_].get('reit_near_count') or 0)
            except (ValueError, TypeError):
                continue
            if b > 0 and a < b * 0.8:
                worse.append((s_, b, a))
        if worse:
            print(f"  [警告] 事例件数が2割以上減った駅 {len(worse)}件: "
                  + ', '.join(f'{s_}({b}->{a})' for s_, b, a in sorted(worse)[:8]))
    else:
        print("\n[前回との差分] 旧 reit_by_station.csv が無いため比較なし")
    # サンプル: 千代田区の数駅で近接が効いているか
    for r in out_rows:
        if r['station'] in ('大手町駅', '麹町駅', '市ケ谷駅'):
            ex = json.loads(r['reit_near_examples'] or '[]')
            print(f"\n{r['station']} (近い順{r['reit_near_count']}件の中央値{r['reit_near_cap_median']}%) 上位3:")
            for e in ex[:3]:
                print(f"  [{e['町名']:<8}] {e['物件'][:22]:<24} cap={e['NOI利回り']}")


if __name__ == '__main__':
    main()
