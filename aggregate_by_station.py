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
"""
import csv
import json
import re
import hashlib
import statistics
import argparse

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
                out[(r['reit_name'], r['property_name'])] = r['location']
    except FileNotFoundError:
        pass
    return out


def to_float(s):
    try:
        return float(s) if s not in (None, '') else None
    except ValueError:
        return None


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
        loc = locmaster.get((r.get('reit_name', ''), r.get('property_name', '')))
        if loc:
            r['location'] = loc
            r['location_source'] = 'REIT公式'
            filled_from_master += 1
        else:
            r['location_source'] = ''

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
            examples.append(item)

        out_rows.append({
            'station': title,
            'reit_cap_median': muni_stat[muni]['cap_median'],
            'reit_count': muni_stat[muni]['count'],
            'reit_examples': json.dumps(examples, ensure_ascii=False),
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
        w = csv.DictWriter(f, fieldnames=['station', 'reit_cap_median', 'reit_count', 'reit_examples'])
        w.writeheader()
        w.writerows(out_rows)

    # ---- 実行サマリ ----
    print(f"駅ごと近接事例を生成: {len(out_rows)}駅 -> {args.out}")
    print(f"  住宅判定 {len(props)}件 / 全{len(all_props)}件")
    print(f"  区が引けて採用 {sum(len(v) for v in by_muni.values())}件")
    print(f"  公式サイト由来で住所を補完: {filled_from_master}件 "
          f"(マスタ {len(locmaster)}件)")
    print(f"  脱落: location欄が空 {drop_no_loc}件 / 東京都外 {drop_other_pref}件")
    if drop_by_reit:
        top = sorted(drop_by_reit.items(), key=lambda x: -x[1])[:5]
        print("  location欠損の多い法人: " + ', '.join(f'{k} {v}件' for k, v in top))

    if prev:
        now = {r['station']: r for r in out_rows}
        added = sorted(set(now) - set(prev))
        removed = sorted(set(prev) - set(now))
        changed = [s_ for s_ in set(now) & set(prev)
                   if now[s_]['reit_examples'] != prev[s_]['reit_examples']
                   or str(now[s_]['reit_count']) != str(prev[s_]['reit_count'])]
        print(f"\n[前回との差分] 駅 追加{len(added)} / 削除{len(removed)} / 内容変化{len(changed)}")
        if removed:
            print(f"  削除された駅(要確認): {removed[:10]}")
        # 件数が大きく減った駅は劣化の疑いがあるので個別に出す
        worse = []
        for s_ in set(now) & set(prev):
            try:
                a, b = int(now[s_]['reit_count']), int(prev[s_]['reit_count'])
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
            ex = json.loads(r['reit_examples'])
            print(f"\n{r['station']} (中央値{r['reit_cap_median']}% / {r['reit_count']}件) 上位3:")
            for e in ex[:3]:
                print(f"  [{e['町名']:<8}] {e['物件'][:22]:<24} cap={e['NOI利回り']}")


if __name__ == '__main__':
    main()
