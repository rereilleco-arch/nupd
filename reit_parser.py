#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EDINET REIT 物件データ パーサー
================================
J-REITの有価証券報告書(EDINET, ordinanceCode=030 / formCode=07B000)から、
物件ごとの「鑑定評価額」「還元利回り(cap rate)」「取得価格」「所在地」「稼働率」等を抽出する。

【重要な設計判断】
- EDINET API の type=5 (CSV変換) は テキストブロックが 30,000文字で切り詰められるため使用不可。
  物件数の多いREIT(例: 平和不動産リート 311物件)では表が丸ごと欠落する。
  → type=1 (生XBRL ZIP) を取得し、XBRL/PublicDoc/*_honbun_*ixbrl.htm を HTMLパースする。
- 物件表の在処は法人により異なる:
    直接保有       → 【投資不動産物件】
    信託受益権保有 → 【その他投資資産の主要なもの】
  どちらに入っていても拾えるよう、"表のヘッダ名" から動的に列を特定する方式にする
  (見出しの位置に依存しない = レイアウトの揺れに強い)。
- rowspan で先頭列(用途区分など)が省略される表があるため、rowspan/colspan を展開してから読む。
- 列は「ヘッダ名」で特定するが、それだけでは法人ごとの多段ヘッダの組み方の違いを
  吸収しきれない。用途については値の側でも語彙チェックを行う(normalize_use)。

【2026-07 修正】実データ5,131件の検査で判明した4件の不具合を修正:
  1. use_type が '資産の種類' を拾い、約500件が「不動産信託受益権」等になっていた
     → asset_type を新設して先に確保させ、use_type のパターンから除去
  2. use_type が COLUMN_PATTERNS 最後尾にあり region 等に列を先取りされていた
     (オリックス不動産121件が全て「東京都心6区」等)
     → use_type を上位へ移動し、値の語彙チェックを追加
  3. 表の外に「(単位:百万円)」がある法人で取得価格が全件捨てられていた
     (いちごオフィス86件/ジャパンリアルエステイト79件/東急28件/ヘルスケア&メディカル69件)
     → caption_unit_scale を追加。単価列(円/坪)が単位判定を壊すのも修正
  4. MONEY_CEILING の境界が > だったため 1,000,000 ちょうどが通過していた
     (ジャパンリアルエステイト「定禅寺パークビル」= 1兆円)  → >= に変更
  併せて acquisition_date を新規抽出(保有期間・年率換算の算出に必要)。

【抽出項目】
  property_name    物件名称
  use_type          用途(正規化: オフィス/住宅/商業/物流/ホテル/ヘルスケア/複合/その他)
  use_type_raw      用途(有報の原文。検証用に必ず残す)
  asset_type        資産の種類(不動産信託受益権 等。用途ではない)
  acquisition_date  取得年月日(ISO)
  acquisition_date_raw / acquisition_date_precision
  acquisition_price 取得価格(百万円)
  book_value        期末帳簿価額(百万円)
  appraisal_value   期末評価額 / 鑑定評価額(百万円)   ★FUDOSAN DBで欠損していた本命
  cap_rate          還元利回り(%)                     ★同上
  appraiser         鑑定評価機関
  location          所在地
  land_area         土地面積(m2)
  gross_floor_area  延床面積(m2)
  occupancy         稼働率(%)
  tenant_count      テナント数
"""

import re
import io
import zipfile
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- テーブル抽出

def table_to_matrix(table):
    """rowspan/colspan を展開して 2次元リストにする。
    (先頭列が rowspan で省略される表が多いため、これをやらないと列がずれる)"""
    # BeautifulSoup の find_all は再帰的なので、<td>の中に<table>が入れ子に
    # なっている表では内側の tr/td まで拾ってしまう。
    # そうなると「外側セルのテキスト(内側セルの連結値)」と「内側セルの個別値」が
    # 同じ行に並び、以降の列が丸ごとずれる。
    # (森ヒルズリートの物件表で確認: 'O-0プレミアム','O-0','プレミアム' が連続し
    #  物件名称の列に所在地が入り、37行中4行しか物件として通らなかった)
    # 直系の tr / td だけを見る。
    rows = [tr for tr in table.find_all('tr') if tr.find_parent('table') is table]
    grid = {}
    for ri, tr in enumerate(rows):
        ci = 0
        cells = [c for c in tr.find_all(['td', 'th']) if c.find_parent('table') is table]
        for cell in cells:
            while (ri, ci) in grid:      # 既に上の行の rowspan で埋まっている
                ci += 1
            text = cell.get_text(strip=True).replace('\u3000', '').replace('\xa0', '')
            try:
                rs = int(cell.get('rowspan', 1))
            except ValueError:
                rs = 1
            try:
                cs = int(cell.get('colspan', 1))
            except ValueError:
                cs = 1
            for r in range(ri, ri + rs):
                for c in range(ci, ci + cs):
                    grid[(r, c)] = text
            ci += cs
    if not grid:
        return []
    maxr = max(r for r, _ in grid) + 1
    maxc = max(c for _, c in grid) + 1
    return [[grid.get((r, c), '') for c in range(maxc)] for r in range(maxr)]


# ---------------------------------------------------------------- 列マッピング
# ヘッダ名の揺れを吸収する。法人ごとに「期末評価額」「鑑定評価額」「期末算定価額」等と表記が違うため、
# 完全一致ではなく正規表現で拾う。順序は「より限定的なものを先に」評価する。

COLUMN_PATTERNS = [
    # (正規化キー, ヘッダ名にマッチする正規表現)
    ('property_name',    r'物件名称|物件名|資産名称|資産名|不動産等の名称|不動産及び信託不動産の名称|不動産の名称|銘柄名|^名称$|^名称|物件の名称'),
    ('property_no',      r'物件番号|物件No|番号'),

    # 【重要】asset_type は use_type より必ず先に置く。
    # 「資産の種類」列の中身は "不動産信託受益権 / 不動産 / 匿名組合出資持分" 等の
    # 保有形態であって用途ではない。以前は use_type のパターンに '資産の種類' が
    # 含まれていたため、この列を用途として取り込んでいた(実データで約500件)。
    # map_columns は used セットで列を1キーにしか割り当てないので、
    # ここで先にこの列を「囮」として確保させ、use_type を本物の用途列に逃がす。
    ('asset_type',       r'資産の種類|資産種別|資産区分|保有形態|不動産等の種類|所有形態'),

    # 【重要】use_type は上位に置く。以前は COLUMN_PATTERNS の最後尾にあり、
    # region(r'地域区分|エリア|地域') 等に先に列を取られた残りを拾っていたため、
    # オリックス不動産投資法人121件が全て「東京都心6区」「その他地域」になっていた。
    # パターンからは '資産の種類'(上に移動)と 'タイプ'(広すぎる)を除去する。
    ('use_type',         r'主?用途区分|主?用途種別|主?用途分類|アセットタイプ|アセット区分|アセットカテゴリ|主要用途|^主?用途'),

    # 取得年月日。旧版には存在しなかったが、combine_headers の docstring にある
    # ヘッダ実例 ['地域区分','用途','不動産等の名称','取得日',...] のとおり
    # 有報の物件表には実在する。保有期間・年率換算の算出に必要。
    ('acquisition_date', r'取得年月日|取得日|取得時期|取得年月|取得の時期'),

    ('acquisition_price',r'取得価格|取得価額'),
    # 帳簿価額: 「期末帳簿価額」「貸借対照表計上額」(日本ビルファンド等)
    ('book_value',       r'期末帳簿価額|帳簿価額|貸借対照表計上額|期末簿価'),
    # 鑑定評価額: 法人により「期末評価額」「鑑定評価額」「期末算定価格/価額」「価格（不動産鑑定評価額）」等
    ('appraisal_value',  r'(不動産鑑定評価額|期末算定価格|期末算定価額|期末評価額|鑑定評価額|期末鑑定評価額|評価額)'),
    # 【順序が重要】「最終還元利回り」は「還元利回り」を含むため、先に最終還元利回りを確定させる。
    # そうしないとcap_rateが最終還元利回りの列を取ってしまう。
    #   なお平和不動産リート等は列名を「還元利回り」とせず、多段ヘッダの
    #   「収益価格 > ＤＣＦ法 > 利回り」という形で最終還元利回りを表す。
    #   (有報の注記:「ＤＣＦ法における『利回り』の欄には最終還元利回りを記載」)
    ('terminal_cap',     r'(最終還元利回り|(?:ＤＣＦ法|DCF法|ＤＣＦ|DCF)[^|]{0,20}利回り)'),
    ('discount_rate',    r'割引率'),
    # cap rate: 必ず「利回り」または「レート」を含む列だけを対象にする。
    # (2段ヘッダ結合で「直接還元法 収益価格(百万円)」のような列ができるため、
    #  「直接還元」だけでマッチさせると金額を利回りとして誤取得する)
    #  平和不動産リート等は「収益価格 > 直接還元法 > 利回り」という多段ヘッダで
    #  還元利回りを表すため、その形も拾う(注記:「直接還元法における『利回り』の欄には
    #  還元利回りを記載」)。「直接還元法 価格」等の金額列は利回りを含まないので誤取得しない。
    ('cap_rate',         r'(直接還元利回り|還元利回り|キャップレート|cap\s*rate|鑑定NOI利回り|NOI利回り|NCF利回り|(?:直接還元法|直接還元)[^|]{0,20}利回り)'),
    ('appraiser',        r'鑑定(評価)?機関|鑑定会社|不動産鑑定'),
    ('investment_ratio', r'投資比率'),
    # 所在地。なお、そもそも物件表に住所を載せない法人が複数ある
    # (ARI・コンフォリア・大和証券リビング・KDX・野村MF 等。地域区分のみ)。
    # それらは有報からは取得不可能なので、公式サイトから reit_locations.csv を
    # 作って aggregate 側で補完する(fetch_reit_locations.py)。
    ('location',         r'所在地|所在場所|所在|住所|地番'),
    # 所在地列を持たない法人(オフィスREIT等)は「地域区分」を持つことがあるので拾う
    ('region',           r'地域区分|エリア|地域'),
    ('rental_income',    r'総賃貸収入|賃貸収入|賃貸事業収入'),
    ('land_area',        r'土地面積|敷地面積'),
    ('gross_floor_area', r'延床面積|延べ床面積'),
    ('leasable_area',    r'総賃貸可能面積|賃貸可能面積'),
    ('leased_area',      r'総賃貸面積|賃貸面積'),
    ('occupancy',        r'稼働率|入居率'),
    ('tenant_count',     r'テナント数|テナント総数|テナントの総数|延べテナント数'),
]

# ---------------------------------------------------------------- 単位の正規化
# 金額の単位は法人により異なる(いちごホテル=百万円 / 日本ビルファンド=千円)。
# ヘッダ文字列から単位を読み取り、すべて「百万円」に統一する。
# これをやらないと日本ビルファンドの金額が1000倍になる(致命的)。

MONEY_FIELDS = ('acquisition_price', 'book_value', 'appraisal_value', 'rental_income')

def detect_unit_scale(header_text):
    """ヘッダ文字列 -> 百万円に直すための倍率。
       '（千円）' -> 0.001 / '（百万円）' -> 1.0 / '（円）' -> 0.000001 / 不明 -> None"""
    if not header_text:
        return None
    h = str(header_text)
    # 単価列(円/坪, 円/m2, 千円/坪 …)は「金額の単位」ではない。
    # これを単位として拾うと unit_scales の「表内で単位が1種類」判定が壊れ、
    # 同じ表の取得価格列が『単位不明』として丸ごと捨てられる。
    if re.search(r'円\s*[／/]\s*(坪|㎡|m2|平米|口|株|人)', h):
        return None
    if re.search(r'百万円', h):
        return 1.0
    if re.search(r'千円', h):
        return 0.001
    if re.search(r'億円', h):
        return 100.0
    # 「(円)」は「百万円」「千円」に含まれないもののみ
    if re.search(r'(?<![百千万])円', h):
        return 0.000001
    return None


# 表の外(キャプション・直前の文)に「(単位:百万円)」と書く法人がある。
# ヘッダにしか単位を求めない旧版では、この形式の取得価格列が
# 『単位不明』として全件捨てられていた(いちごオフィス86件・ジャパンリアルエステイト79件・
#  東急リアル・エステート28件・ヘルスケア&メディカル69件で取得価格の充足率が0〜1%)。
# 誤爆すると1000倍事故になるため、「単位」という語を伴う明示的な表記だけを信用する。
_CAPTION_UNIT_RX = re.compile(r'単位[：:\s]*[（(]?\s*(百万円|千円|億円|円)\s*[）)]?')

def caption_unit_scale(context_text):
    """表のキャプション等から『単位：○円』を読む。無ければ None。"""
    if not context_text:
        return None
    m = _CAPTION_UNIT_RX.search(str(context_text))
    if not m:
        return None
    return {'百万円': 1.0, '千円': 0.001, '億円': 100.0, '円': 0.000001}[m.group(1)]


def unit_scales(header_row, mapping, context_text=None):
    """金額列ごとの倍率を返す {field: scale}。
    単位がヘッダに明記されていない金額列には、同一表の単位を継承する。
    (1つの鑑定表内で千円と百万円が混在することは実務上ないため安全)

    継承元は2段階で探す:
      1) 単位既知の金額列(MONEY_FIELDS)の単位
      2) それが無ければ、ヘッダ行全体に現れる単位トークン(千円/百万円/億円)
         例: スターツプロシードの鑑定評価額列は単位表記が無いが、同じ表に
             「直接還元法による価格(千円)」列があるので千円と判断できる
    """
    out = {}
    known = []
    for f in MONEY_FIELDS:
        ci = mapping.get(f)
        if ci is None or ci >= len(header_row):
            continue
        s = detect_unit_scale(header_row[ci])
        if s is not None:
            out[f] = s
            known.append(s)

    # 継承元の単位を決める。安全のため「ヘッダ行全体で単位が1種類に定まる」ときのみ継承する。
    # (千円と百万円が混在する表では継承しない=暴発防止)
    inherit = None
    found = set()
    for h in header_row:
        s = detect_unit_scale(h)
        if s is not None:
            found.add(s)
    if len(found) == 1:
        inherit = next(iter(found))

    # 3) ヘッダから決まらない場合のみ、表の外の「単位：○円」を使う。
    #    (ヘッダに書いてある単位を常に優先する = 既存の挙動を変えない)
    if inherit is None:
        inherit = caption_unit_scale(context_text)

    if inherit is not None:
        for f in MONEY_FIELDS:
            ci = mapping.get(f)
            if ci is not None and ci < len(header_row) and f not in out:
                out[f] = inherit
    return out

def map_columns(header_row):
    """ヘッダ行 -> {正規化キー: 列index}。1つの列が複数キーにマッチしないよう、先に決まったものを優先。"""
    mapping = {}
    used = set()
    for key, pat in COLUMN_PATTERNS:
        rx = re.compile(pat, re.I)
        for ci, h in enumerate(header_row):
            if ci in used or not h:
                continue
            if rx.search(h):
                mapping[key] = ci
                used.add(ci)
                break
    return mapping


def is_property_table(mapping, ncols):
    """物件表とみなす条件: 物件名があり、かつ物件固有の情報を1つ以上持つ。
    価格系(取得価格/鑑定評価額/cap rate)だけでなく、稼働率・面積・テナント数を持つ
    補助表(日本ビルファンドの稼働率表など。所在地列が無い法人もある)も対象に含める。"""
    if 'property_name' not in mapping:
        return False
    signals = ('appraisal_value', 'cap_rate', 'acquisition_price', 'book_value',
               'location', 'occupancy', 'leasable_area', 'leased_area',
               'gross_floor_area', 'land_area', 'tenant_count')
    return any(k in mapping for k in signals)


# 取得予定・譲渡予定の表は「保有物件」ではないため除外する。
# (これらも「価格(不動産鑑定評価額)」列を持つため、除外しないと未取得物件が混入する)
EXCLUDE_HEADER_RX = re.compile(r'取得予定|譲渡予定|売却予定|処分予定')

def is_excluded_table(header_row):
    return any(EXCLUDE_HEADER_RX.search(h or '') for h in header_row)


# ---------------------------------------------------------------- 値の正規化

def to_num(s):
    """'4,480' -> 4480.0 / '4.3' -> 4.3 / '－','—','-','' -> None"""
    if s is None:
        return None
    s = str(s).strip()
    if s in ('', '-', '－', '—', '―', '–', 'N/A', '該当事項はありません'):
        return None
    s = s.replace(',', '').replace('，', '')
    # 注記を除去(全角/半角括弧・全角数字に対応)
    s = re.sub(r'[（(]\s*注[\s\d０-９，、,.・]*\s*[）)]', '', s)
    s = re.sub(r'[^\d.\-]', '', s)                    # 単位・記号を除去
    if s in ('', '-', '.'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_name(s):
    """物件名から注記を除去する。
    法人により表記が揺れる: （注1）(注１) （注10） (注2、3) 等。
    主表と補助表で注記の有無が違うため(例: 主表「新宿三井ビルディング」/
    稼働率表「新宿三井ビルディング(注１)」)、ここを揃えないとマージに失敗する。"""
    if not s:
        return ''
    s = str(s)
    # 全角/半角の括弧、全角/半角の数字、複数注記(注1、2)に対応
    s = re.sub(r'[（(]\s*注[\s\d０-９，、,.・]*\s*[）)]', '', s)
    # 「※1」「*1」形式も除去
    s = re.sub(r'[※*][\d０-９]+', '', s)
    return s.strip()


# ---------------------------------------------------------------- 用途の正規化
# 列の取り違えは header 側の修正だけでは防ぎきれない(法人ごとに多段ヘッダの
# 組み方が違うため)。値の側でも語彙チェックを行い、用途語でないものは捨てる。
# これが最後の砦。旧版の出力に「東京都心6区」「フォレストタワー(住宅棟)」
# 「R-4」「不動産信託受益権」が用途として入っていたのを止める。
#
# 原文は use_type_raw に必ず残す(粒度を偽らない / 後から検証できるようにする)。

USE_VOCAB = [
    # (正規化後, 判定パターン) 上から順に評価する。順序が意味を持つ:
    #  ・ヘルスケアを先に見る(「ヘルスケアレジデンス」を住宅と誤判定しないため)
    #  ・複合を住宅/オフィスより先に見る(「オフィス・商業複合」を拾うため)
    ('ヘルスケア', r'ヘルスケア|介護|老人ホーム|高齢者|医療|病院|サービス付き高齢者'),
    ('複合',       r'複合|混合|多用途'),
    ('ホテル',     r'ホテル|宿泊|旅館|リゾート施設'),
    ('物流',       r'物流|倉庫|インダストリアル|工業|産業用|配送'),
    ('商業',       r'商業|店舗|小売|ショッピング|リテール'),
    ('オフィス',   r'事務所|オフィス|業務'),
    ('住宅',       r'住宅|住居|居住|共同住宅|レジデンス|居宅|賃貸住宅|アパート'),
    ('その他',     r'駐車場|データセンター|教育|研究所|インフラ|発電'),
]

def normalize_use(raw):
    """有報の用途欄の文字列 -> 正規化用途。用途語彙に無ければ None(=採用しない)。"""
    if not raw:
        return None
    s = str(raw)
    # 「(注1)」「※1」等は clean_name で落ちている前提だが念のため
    s = re.sub(r'[（(][^（()）]*[)）]', '', s).strip()
    if not s:
        return None
    for canon, pat in USE_VOCAB:
        if re.search(pat, s):
            return canon
    return None


# ---------------------------------------------------------------- 取得年月日
# 有報の表記ゆれ: 2007年12月3日 / 2007/12/3 / 2007.12.3 / 平成19年12月3日
# 出力は ISO(YYYY-MM-DD)に統一する。日が無い場合は月初を補って粒度を date_precision で持つ。

_ERA_BASE = {'令和': 2018, '平成': 1988, '昭和': 1925}

def to_date(s):
    """日付文字列 -> (ISO日付, 精度) / 読めなければ (None, None)。精度は 'day'|'month'。"""
    if not s:
        return (None, None)
    t = str(s).strip()
    t = re.sub(r'[（(][^（()）]*[)）]', '', t)
    t = t.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    # 和暦
    m = re.search(r'(令和|平成|昭和)\s*(\d{1,2}|元)\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?', t)
    if m:
        era, yy, mm, dd = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        yy = 1 if yy == '元' else int(yy)
        y = _ERA_BASE[era] + yy
        return (f'{y:04d}-{mm:02d}-{int(dd):02d}', 'day') if dd else (f'{y:04d}-{mm:02d}-01', 'month')
    # 西暦
    m = re.search(r'(\d{4})\s*[年/.\-]\s*(\d{1,2})(?:\s*[月/.\-]\s*(\d{1,2}))?', t)
    if m:
        y, mm, dd = int(m.group(1)), int(m.group(2)), m.group(3)
        if not (1990 <= y <= 2100) or not (1 <= mm <= 12):
            return (None, None)
        if dd and 1 <= int(dd) <= 31:
            return (f'{y:04d}-{mm:02d}-{int(dd):02d}', 'day')
        return (f'{y:04d}-{mm:02d}-01', 'month')
    return (None, None)


SKIP_ROW_RX = re.compile(r'(^(合計|小計|総計|中計|平均|合計／平均|ポートフォリオ|計)'
                         r'|計$|物件計|物件$|区計|地区計|エリア計|用途計|合計欄)')

def is_skip_row(name):
    n = (name or '').strip()
    if not n:
        return True
    if SKIP_ROW_RX.search(n):
        return True
    # 「東京23区計」「中計(計155物件)」等、"計"+数字+"物件"を含む集計行
    if re.search(r'計.*物件|物件.*計|\d+物件', n):
        return True
    # 物件番号だけの行(例: Ａ01 / Ｇ03 / A-1)。物件名の列が取れず物件番号を
    # キーにしてしまった表(稼働率推移表・損益状況表など)から混入する。
    # 名前として表示できず、他の指標も別表と突合できないため取り込まない。
    # (ＮＴＴ都市開発リートで、番号行62件に誤った利回りが入る事象を確認)
    if re.fullmatch(r'[A-ZＡ-Ｚa-zａ-ｚ]{1,2}[-－ー]?[0-9０-９]{1,3}', n):
        return True
    return False


# 海外物件の判定(所在地に海外地名を含む)。noitasは東京の不動産DBのため海外物件は除外。
# 外貨建て金額を円として読むと桁が壊れるので、単位ずれ防止の意味もある。
OVERSEAS_RX = re.compile(
    r'ケイマン|マカオ|ハワイ|グアム|サイパン|米国|アメリカ|シンガポール|香港|上海|北京|'
    r'ベトナム|タイ王国|マレーシア|インドネシア|フィリピン|豪州|オーストラリア|英国|英領|'
    r'ドイツ|フランス|オランダ|カナダ|韓国|台湾|海外')

def is_overseas(location):
    return bool(location and OVERSEAS_RX.search(str(location)))


# 鑑定評価額・取得価格の現実的上限(百万円)。1件1兆円超は単位ずれ・合計行・外貨建て。
MONEY_CEILING_MILLION = 1_000_000

# 鑑定評価額が帳簿価額の何倍を超えたら破損とみなすか。
# 実データ上、正常物件の鑑定/簿価比は最大4.3倍(底地・築古で簿価が償却されたもの)、
# 破損値(セルに別数値が連結)は10倍以上に集中しているため、10倍で切ると正常値を巻き込まない。
APPRAISAL_BOOK_RATIO_MAX = 10.0


# ---------------------------------------------------------------- 本体

NUM_FIELDS = ('acquisition_price', 'book_value', 'appraisal_value', 'cap_rate',
              'land_area', 'gross_floor_area', 'leasable_area', 'leased_area',
              'occupancy', 'tenant_count', 'investment_ratio', 'rental_income',
              'discount_rate', 'terminal_cap')

# 主表(物件の母集団を決める表)の条件: 取得価格 or 鑑定評価額 or cap rate を持つ
PRIMARY_KEYS = ('acquisition_price', 'appraisal_value', 'cap_rate')

def _read_table(matrix, header, mapping, data_start, context_text=None):
    """1つの表を読んで {物件名: {field: value}} を返す。金額は百万円に正規化する。"""
    scales = unit_scales(header, mapping, context_text)
    out = {}
    for row in matrix[data_start:]:
        if len(row) <= mapping['property_name']:
            continue
        name = clean_name(row[mapping['property_name']])
        if not name or is_skip_row(name):
            continue
        # ヘッダの繰り返し行(2段ヘッダの残骸)を除外
        if name in ('物件名称', '物件名', '不動産等の名称', '資産名'):
            continue
        rec = out.setdefault(name, {'property_name': name})
        for key, ci in mapping.items():
            if key == 'property_name' or ci >= len(row):
                continue
            raw = row[ci]
            if raw in ('', '-', '－', '—'):
                continue
            if key in NUM_FIELDS:
                v = to_num(raw)
                if v is None:
                    continue
                if key in MONEY_FIELDS:
                    sc = scales.get(key)
                    if sc is None:
                        # 単位不明の金額は取り込まない(1000倍事故を防ぐ)
                        continue
                    v = v * sc
                rec[key] = v
            elif key == 'use_type':
                # 原文は必ず残し、用途語彙に無い値は use_type に採用しない。
                # (列の取り違えが header 側の修正をすり抜けた場合の最後の砦)
                v = clean_name(raw)
                if not v:
                    continue
                rec.setdefault('use_type_raw', v)
                canon = normalize_use(v)
                if canon:
                    rec.setdefault('use_type', canon)
            elif key == 'acquisition_date':
                v = clean_name(raw)
                if not v:
                    continue
                rec.setdefault('acquisition_date_raw', v)
                iso, prec = to_date(v)
                if iso:
                    rec.setdefault('acquisition_date', iso)
                    rec.setdefault('acquisition_date_precision', prec)
            else:
                v = clean_name(raw)
                if v:
                    rec.setdefault(key, v)
    return out


def table_context_text(table, lookback=3, maxlen=300):
    """表のキャプションと直前のテキストを返す。『単位：百万円』が表の外にある法人向け。
    直前3ノードまでに限定する(遠くの別の表の単位を拾わないため)。"""
    parts = []
    cap = table.find('caption')
    if cap:
        parts.append(cap.get_text(strip=True))
    node, seen = table, 0
    while seen < lookback:
        node = node.find_previous_sibling()
        if node is None:
            break
        t = node.get_text(strip=True) if hasattr(node, 'get_text') else str(node).strip()
        if t:
            if node.name == 'table':      # 直前が別の表なら、そこで打ち切る
                break
            parts.append(t[:maxlen])
            seen += 1
    return ' '.join(parts)[:maxlen * 2]


def combine_headers(matrix, hi, depth=2):
    """hi行目から depth行ぶんのヘッダを結合して1つのヘッダ行にする。

    多くのREITが2段ヘッダを使う:
        1行目: ['地域区分','用途','不動産等の名称','取得日','取得価格','取得価格','期末評価額','期末評価額']
        2行目: [  '',      '',        '',        '',  '価格（百万円）','投資比率（％）','評価額（百万円）','投資比率（％）']
    これを結合して ['...','取得価格 価格（百万円）','取得価格 投資比率（％）','期末評価額 評価額（百万円）',...]
    とすることで、列の意味と単位を同時に読めるようにする。
    1段ヘッダの表でも、2行目が空なら結果は変わらないので安全。
    """
    if hi >= len(matrix):
        return []
    rows = matrix[hi:hi + depth]
    ncols = max(len(r) for r in rows)
    out = []
    for c in range(ncols):
        parts = []
        for r in rows:
            v = r[c] if c < len(r) else ''
            if v and v not in parts:
                parts.append(v)
        out.append(' '.join(parts))
    return out


def disambiguate_yield_headers(header):
    """多段ヘッダ結合後、単に「利回り」としか書かれていない列を直前列から判別する。

    平和不動産リート等は「直接還元法」「ＤＣＦ法」を利回り列の親ではなく
    *隣の列*(価格列)の見出しとして置くため、結合すると
        col: '… 収益価格（百万円） 直接還元法'   ← 直接還元法の価格
        col: '… 収益価格（百万円） 利回り(注3)'  ← 還元利回り
        col: '… 収益価格（百万円） ＤＣＦ法'      ← DCF法の価格
        col: '… 収益価格（百万円） 利回り(注3)'  ← 最終還元利回り
    となり、2つの利回り列が同一文字列になって区別できない。
    有報の注記どおり「直接還元法の次の利回り＝還元利回り」「ＤＣＦ法の次の利回り＝
    最終還元利回り」として、ヘッダ文字列を書き換えて後段のマッチに回す。
    """
    out = list(header)
    for i, h in enumerate(out):
        if '利回り' not in h:
            continue
        # 既に種別が明示されている列は触らない
        if re.search(r'還元利回り|割引率|NOI|NCF|キャップレート', h):
            continue
        prev = out[i - 1] if i > 0 else ''
        if re.search(r'ＤＣＦ|DCF', prev):
            out[i] = h + ' 最終還元利回り'
        elif re.search(r'直接還元', prev):
            out[i] = h + ' 還元利回り'
    return out


# ヘッダ判定の前に取り除く「注記番号」「単位」などの括弧書き。
# 例: 「利回り(注3)」「鑑定評価額(百万円)(注2)」「還元利回り（％）」
_HEADER_PAREN_RX = re.compile(r'[（(][^（()）]*[)）]')


def _is_header_like(row):
    """その行がヘッダの続き(2段目以降)に見えるか。
    数値が1つでも含まれる行はデータ行とみなす(ヘッダに数値は基本入らない)。
    ※データ行をヘッダと誤認すると、その行の物件が丸ごと欠落する。

    ただし「利回り(注3)」「鑑定評価額(百万円)(注2)」のように、
    注記番号や単位を括弧書きで持つヘッダは実在する(平和不動産リート等)。
    括弧の中身を除いてから数値判定しないと、多段ヘッダを1段しか読めず、
    「直接還元法／利回り」等の列名を取りこぼす。
    """
    nonempty = [c for c in row if c]
    if not nonempty:
        return False   # 空行はヘッダ2段目ではない(結合しても無意味)
    for c in nonempty:
        stripped = _HEADER_PAREN_RX.sub('', c).strip()
        if not stripped:
            continue   # 括弧だけのセル(単位のみ等)は判定材料にしない
        if to_num(stripped) is not None:
            return False   # 括弧を除いても数値 = データ行
    return True


def parse_property_tables(html):
    """本文HTMLから物件表を拾い、物件名をキーにマージした dict を返す。

    2段構え:
      1) 主表(取得価格/鑑定評価額/cap rateを持つ表)から物件の母集団を確定する
      2) 補助表(所在地・稼働率など)は、母集団に載っている物件だけにマージする
    ヘッダは2行結合して読む(法人の多くが2段ヘッダを使うため)。
    """
    soup = BeautifulSoup(html, 'lxml')
    tables = soup.find_all('table')

    parsed = []
    for ti, table in enumerate(tables):
        matrix = table_to_matrix(table)
        if len(matrix) < 3:
            continue
        best = None
        for hi in (0, 1):
            if hi >= len(matrix) - 1:
                continue
            # ヘッダが複数段になっている表(最大4段)に対応するため、
            # 「次の行がヘッダらしい(数値を含まない)」限り結合を続ける。
            # 例: スターツプロシードは4段ヘッダ
            #   行0 不動産鑑定評価概要 / 行1 収益価格 / 行2 直接還元法・DCF法 / 行3 還元利回り(％)等
            depth = 1
            while (hi + depth < len(matrix) and depth < 4
                   and _is_header_like(matrix[hi + depth])):
                depth += 1
            header = combine_headers(matrix, hi, depth)
            header = disambiguate_yield_headers(header)
            mapping = map_columns(header)
            if not is_property_table(mapping, len(header)):
                continue
            if is_excluded_table(header):
                continue
            # データ開始行 = ヘッダの次
            data_start = hi + depth
            score = len(mapping)
            if best is None or score > best[0]:
                best = (score, hi, depth, header, mapping, data_start)
        if best is None:
            continue
        _, hi, depth, header, mapping, data_start = best
        is_primary = any(k in mapping for k in PRIMARY_KEYS)
        ctx = table_context_text(table)
        parsed.append((ti, header, mapping, matrix, data_start, is_primary, ctx))

    merged = {}
    tables_used = []

    # 1) 主表で母集団を確定
    for ti, header, mapping, matrix, data_start, is_primary, ctx in parsed:
        if not is_primary:
            continue
        got = _read_table(matrix, header, mapping, data_start, ctx)
        if not got:
            continue
        for name, rec in got.items():
            tgt = merged.setdefault(name, {'property_name': name})
            for k, v in rec.items():
                if k not in tgt or tgt[k] in ('', None):
                    tgt[k] = v
        tables_used.append((ti, sorted(mapping.keys()), len(got), 'primary'))

    # 2) 補助表は、母集団に存在する物件にだけマージ
    for ti, header, mapping, matrix, data_start, is_primary, ctx in parsed:
        if is_primary:
            continue
        got = _read_table(matrix, header, mapping, data_start, ctx)
        hit = 0
        for name, rec in got.items():
            if name not in merged:
                continue
            for k, v in rec.items():
                if k not in merged[name] or merged[name][k] in ('', None):
                    merged[name][k] = v
            hit += 1
        if hit:
            tables_used.append((ti, sorted(mapping.keys()), hit, 'merge'))

    # 3) 縦型「1物件=1テーブル」形式からの補完。
    #    いちご等は取得価格・取得年月日を横組み一覧に載せず、物件個別の縦テーブルに書く。
    #    横組みで母集団は取れているので、欠けている項目(主に取得価格)だけを埋める。
    #    名寄せは完全一致→スペース/全半角無視の順(有報内でも表記が揺れるため)。
    vert = collect_vertical_properties(html)
    if vert:
        import unicodedata
        def _nk(s):
            return re.sub(r'[\s\u3000]+', '', unicodedata.normalize('NFKC', str(s or '')))
        norm_index = { _nk(n): n for n in merged }
        vfilled = 0
        for vname, vrec in vert.items():
            target = vname if vname in merged else norm_index.get(_nk(vname))
            if not target:
                continue
            for k, v in vrec.items():
                if k == 'property_name':
                    continue
                if k not in merged[target] or merged[target][k] in ('', None):
                    merged[target][k] = v
                    vfilled += 1
        if vfilled:
            tables_used.append(('vertical', ['acquisition_price', 'acquisition_date'], vfilled, 'vertical'))

    # 事後フィルタ: 海外物件を除外し、金額が非現実的な物件は該当項目を落とす
    cleaned = {}
    for name, rec in merged.items():
        loc = rec.get('location', '') or rec.get('region', '')
        if is_overseas(loc):
            continue   # 海外物件はnoitasの対象外(外貨建てで桁も壊れる)
        # 桁が壊れた金額(合計行の取りこぼし・単位ずれ)は個別に落とす
        for mf in MONEY_FIELDS:
            v = rec.get(mf)
            if v is not None and v >= MONEY_CEILING_MILLION:
                rec.pop(mf, None)
        # 鑑定評価額が帳簿価額の10倍超 = セルに別数値が連結された破損値とみなし落とす
        # (正常な底地・築古でも4.3倍程度が上限。10倍超は破損)
        av, bv = rec.get('appraisal_value'), rec.get('book_value')
        if av is not None and bv is not None and bv > 0 and av / bv > APPRAISAL_BOOK_RATIO_MAX:
            rec.pop('appraisal_value', None)
        cleaned[name] = rec

    return cleaned, tables_used


def fetch_honbun_htmls(doc_id, api_key, timeout=120):
    """EDINET から type=1 (生XBRL) を取得し、本文HTMLを『全て』返す。

    REITの有報は本文が複数ファイルに分割されていることがあり
    (0101010_honbun / 0102010_honbun / ...)、物件表と鑑定評価表が
    別ファイルに入る法人がある。最大の1ファイルだけを読むと取りこぼすため、
    全ての本文HTMLを返して呼び出し側でマージする。
    """
    import requests
    r = requests.get(
        f"https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}",
        params={"type": 1, "Subscription-Key": api_key}, timeout=timeout)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    cands = [n for n in z.namelist()
             if '/PublicDoc/' in n and '_honbun_' in n and n.endswith('.htm')]
    if not cands:
        raise RuntimeError(f"本文HTMLが見つかりません: {z.namelist()[:10]}")
    # 大きいファイル順(物件表は大きいファイルにあることが多い)
    cands.sort(key=lambda n: len(z.read(n)), reverse=True)
    return [z.read(n).decode('utf-8', errors='replace') for n in cands]


def fetch_honbun_html(doc_id, api_key, timeout=120):
    """後方互換: 最大の本文HTMLを1つだけ返す。"""
    return fetch_honbun_htmls(doc_id, api_key, timeout)[0]


def merge_property_maps(maps):
    """複数ファイルのパース結果をマージする。

    【方針】最初のファイル(=最大の本文HTML。物件表が入っている)で見つかった物件だけを対象とし、
    後続ファイルは「既に見つかっている物件の、欠けている項目を埋める」用途に限定する。
    後続ファイルから新規の物件名を追加すると、本文中の物件一覧(金額を持たない列挙)まで
    拾ってしまい、中身の無い行が増えて充足率が下がるため(実データで確認済み)。
    """
    if not maps:
        return {}
    merged = {name: dict(rec) for name, rec in maps[0].items()}
    for m in maps[1:]:
        for name, rec in m.items():
            if name not in merged:
                continue                      # 新規物件は追加しない(ノイズ防止)
            base = merged[name]
            for k, v in rec.items():
                if v is None or v == '':
                    continue
                if base.get(k) is None or base.get(k) == '':
                    base[k] = v               # 欠けている項目だけ補完
    return merged


# ============================================================================
# 縦型「1物件=1テーブル」形式への対応
# ----------------------------------------------------------------------------
# いちごオフィス等は、物件個別ページを縦組みの小テーブルで書く:
#     行: [特定資産の種類, 不動産信託受益権]
#     行: [取得価格,       3,254百万円]
#     行: [取得年月日,     2011年11月１日]
# これは横組みの物件表ではないため is_property_table を通らず、取得価格が
# 丸ごと欠落していた(いちご86件/東急28件/ヘルスケア&メディカル69件で0%)。
#
# 各小テーブルには物件名が入っていないことが多く、直前の見出し(通常は
# 「[いちご西参道ビル]」のような帯、または直近の物件名テキスト)から取る。
# 取れた (物件名, 取得価格, 取得年月日) を横組み一覧表の母集団にマージする。

_VERT_LABELS = {
    'acquisition_price': re.compile(r'^取得価格|^取得価額|^投資額'),
    'acquisition_date':  re.compile(r'^取得年月日|^取得日'),
    'appraisal_value':   re.compile(r'^(期末)?鑑定評価額|^期末算定価額'),
    'book_value':        re.compile(r'^(期末)?帳簿価額|^帳簿価額'),
    'location':          re.compile(r'^所在地|^所在'),
}
# この形式を「物件個別テーブル」と判断する最低条件: 取得価格の行がある
_VERT_REQUIRED = 'acquisition_price'


def _looks_vertical(matrix):
    """先頭列が項目名・2列目が値、という縦組みの物件テーブルか判定。
    取得価格の行を持つものだけを対象にする(集計表や手数料表を除くため)。"""
    if len(matrix) < 3:
        return False
    labels = [str(row[0]).strip() if row else '' for row in matrix]
    if not any(_VERT_LABELS[_VERT_REQUIRED].search(lb) for lb in labels):
        return False
    # 「取得価格」ラベル行の2列目が金額らしいこと
    for row in matrix:
        if row and _VERT_LABELS['acquisition_price'].search(str(row[0])):
            val = row[1] if len(row) > 1 else ''
            if re.search(r'[\d,]{3,}\s*(百万円|千円|円|億円)', str(val)):
                return True
    return False


def _read_vertical(matrix):
    """縦組みテーブルから項目を1件ぶん読む。物件名は入っていれば拾う。"""
    rec = {}
    name = None
    for row in matrix:
        if not row:
            continue
        label = str(row[0]).strip()
        value = str(row[1]).strip() if len(row) > 1 else ''
        # 物件名ラベル
        if re.search(r'物件名|名称|物件の名称', label) and value:
            name = clean_name(value)
            continue
        for key, rx in _VERT_LABELS.items():
            if rx.search(label) and value:
                if key in ('acquisition_price', 'appraisal_value', 'book_value'):
                    scale = detect_unit_scale(value) or (
                        0.001 if '千円' in value else
                        100.0 if '億円' in value else
                        1.0 if '百万円' in value else None)
                    num = to_num(value)
                    if num is not None and scale is not None:
                        rec[key] = round(num * scale, 3)
                elif key == 'acquisition_date':
                    iso, prec = to_date(value)
                    if iso:
                        rec['acquisition_date'] = iso
                        rec['acquisition_date_raw'] = value
                        rec['acquisition_date_precision'] = prec
                else:
                    rec[key] = clean_name(value)
                break
    return name, rec


def _heading_before(table):
    """縦テーブルの直前にある物件名の見出しを探す。
    「[いちご西参道ビル]」「(1) いちご三田ビル」等の帯を想定。"""
    node = table
    for _ in range(6):
        node = node.find_previous_sibling() if node else None
        if node is None:
            break
        text = node.get_text(strip=True) if hasattr(node, 'get_text') else str(node).strip()
        if not text or len(text) > 60:
            continue
        # 角括弧で囲まれた物件名 or 末尾が「ビル/タワー/レジデンス」等
        m = re.search(r'[\[［]([^\]］]{3,40})[\]］]', text)
        if m:
            return clean_name(m.group(1))
        if re.search(r'(ビル|タワー|レジデンス|ハウス|プラザ|センター|スクエア|パーク|ヒルズ|マンション)\s*$', text):
            return clean_name(re.sub(r'^[\(（]?[\d０-９]+[\)）]?\s*', '', text))
    return None


def collect_vertical_properties(html):
    """縦型テーブルから {物件名: {取得価格, 取得年月日, ...}} を返す。
    横組み一覧が母集団を持つ前提で、取得価格等の欠落を補完する用途。"""
    soup = BeautifulSoup(html, 'lxml')
    out = {}
    for table in soup.find_all('table'):
        matrix = table_to_matrix(table)
        if not _looks_vertical(matrix):
            continue
        name, rec = _read_vertical(matrix)
        if not rec:
            continue
        if not name:
            name = _heading_before(table)
        if not name:
            continue
        tgt = out.setdefault(name, {'property_name': name})
        for k, v in rec.items():
            tgt.setdefault(k, v)
    return out
