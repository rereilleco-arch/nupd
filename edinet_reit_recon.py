#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EDINET REIT AUM自動化 - Phase 1: 偵察スクリプト
=================================================
noitas.com のREIT個別ページ(eki_reit_overrides)にあるAUM(運用資産残高)を、
EDINET(金融庁)の一次データから自動更新するための"下調べ"を行うスクリプト。

このスクリプトは、まだAUMの本番自動更新はしない。以下3つを確認するためのもの:

  A. reit_names.json(現存59+廃止8=67法人、eki-master-template.phpから機械的に抽出)の
     法人名を、EDINET書類一覧APIの実際の提出実績(filerName)と突合し、
     「REIT名 -> edinetCode」の対応表を作る。過去N日分を1日ずつ舐めて実データで確定させる
     （EDINETコードリストCSVのダウンロードはJS経由の複雑なフォームのため、ここでは使わない）。

  B. 突合できた書類の ordinanceCode/formCode/docTypeCode/docDescription を集計し、
     J-REIT(投資法人)の定期開示が実際にどの書類種別(有価証券報告書 or 半期報告書等)・
     どのコードで来ているかを確認する。ここは「030(特定有価証券)」だろうという推測はしつつも、
     決め打ちせず実データで確認する。

  C. 代表1社の最新書類をCSV形式(EDINET API type=5、UTF-16タブ区切り)で取得し、
     「項目名」に"資産"を含む行を一覧表示する。AUM(総資産額)として使うべき
     項目名/要素ID/相対年度/連結・個別の組み合わせは、ここで人間の目で最終確認する。

前提:
  - EDINET APIキー(無料。メールアドレス+電話番号で登録、運営者ご自身で登録してください):
    https://api.edinet-fsa.go.jp/ の「新規登録」から取得
  - 環境変数 EDINET_API_KEY にセット
  - pip install requests pandas --break-system-packages
  - reit_names.json を同じフォルダに置く

実行例:
  export EDINET_API_KEY=xxxxxxxx
  python3 edinet_reit_recon.py scan --days 400
  python3 edinet_reit_recon.py sample --name "日本ビルファンド投資法人"

出力:
  scan   -> edinet_reit_matches.csv (法人ごとの突合結果) / edinet_unmatched.txt (未突合法人) /
            edinet_doctype_summary.csv (実際に出現した書類種別コードの集計)
  sample -> edinet_sample_<法人名>_assets.csv (資産関連行の一覧)

注意:
  - scanはEDINET書類一覧APIを1日1回呼ぶため、400日指定で約400リクエスト。
    --sleep(既定0.4秒)のインターバルを空けて実行するので、数分かかる。
  - 名前の突合は「投資法人」等の固有名詞をベースにしているため誤検出は稀と思われるが、
    edinet_reit_matches.csv は必ず目視確認してから次工程(本番AUM取得スクリプト化)に進むこと。
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import requests

API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
API_KEY = os.environ.get("EDINET_API_KEY", "")
HERE = Path(__file__).resolve().parent
NAMES_FILE = HERE / "reit_names.json"


def load_names():
    with open(NAMES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    entries = []
    for c in data["current"]:
        aliases = {c["name"]}
        if c.get("note"):
            aliases.add(c["note"])
        entries.append({"canonical": c["name"], "status": "現存", "aliases": aliases})
    for d in data["defunct"]:
        aliases = {d["name"]}
        entries.append({"canonical": d["name"], "status": "廃止", "aliases": aliases})
    return entries


# 空白・中黒・各種ダッシュ・括弧類を除去して比較する(全角/半角の揺れはここでは吸収しない=過剰マッチ回避)
_STRIP_RE = re.compile(r"[\s\u3000・･\-–—ー()（）\[\]「」『』]+")


def norm(s):
    return _STRIP_RE.sub("", str(s or ""))


def build_alias_index(entries):
    idx = {}
    for e in entries:
        for a in e["aliases"]:
            idx[norm(a)] = e
    return idx


def match_filer(filer_name, alias_index):
    n = norm(filer_name)
    if not n:
        return None
    if n in alias_index:
        return alias_index[n]
    for key, e in alias_index.items():
        if len(key) >= 4 and (key in n or n in key):
            return e
    return None


def api_get(path, params, retries=3):
    if not API_KEY:
        sys.exit("環境変数 EDINET_API_KEY を設定してください")
    params = dict(params)
    params["Subscription-Key"] = API_KEY
    last = None
    for attempt in range(retries):
        r = requests.get(f"{API_BASE}/{path}", params=params, timeout=30)
        if r.status_code == 200:
            return r
        if r.status_code == 404:
            return None
        last = r
        time.sleep(2 * (attempt + 1))
    if last is not None:
        last.raise_for_status()
    return None


def cmd_scan(args):
    entries = load_names()
    alias_index = build_alias_index(entries)
    matches = {}
    doctype_counter = {}

    start = date.today() - timedelta(days=args.days)
    d = start
    checked = 0
    while d <= date.today():
        checked += 1
        r = api_get("documents.json", {"date": d.isoformat(), "type": 2})
        if r is not None:
            try:
                data = r.json()
            except Exception:
                data = None
            if data and data.get("results"):
                for doc in data["results"]:
                    filer = doc.get("filerName") or ""
                    e = match_filer(filer, alias_index)
                    if e:
                        rec = {
                            "canonical": e["canonical"],
                            "status": e["status"],
                            "filerName": filer,
                            "edinetCode": doc.get("edinetCode"),
                            "docID": doc.get("docID"),
                            "ordinanceCode": doc.get("ordinanceCode"),
                            "formCode": doc.get("formCode"),
                            "docTypeCode": doc.get("docTypeCode"),
                            "docDescription": doc.get("docDescription"),
                            "periodStart": doc.get("periodStart"),
                            "periodEnd": doc.get("periodEnd"),
                            "submitDateTime": doc.get("submitDateTime"),
                            "csvFlag": doc.get("csvFlag"),
                            "xbrlFlag": doc.get("xbrlFlag"),
                        }
                        matches.setdefault(e["canonical"], []).append(rec)
                        key = (
                            doc.get("ordinanceCode"),
                            doc.get("formCode"),
                            doc.get("docTypeCode"),
                            (doc.get("docDescription") or "")[:20],
                        )
                        doctype_counter[key] = doctype_counter.get(key, 0) + 1
        if checked % 20 == 0:
            print(f"  {checked}/{args.days}日 走査済み... (現在{len(matches)}法人ヒット)", file=sys.stderr)
        time.sleep(args.sleep)
        d += timedelta(days=1)

    with open("edinet_reit_matches.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "REIT名(サイト表記)", "現存/廃止", "EDINETコード", "書類件数",
            "最新提出日", "最新docID", "最新docDescription", "最新formCode", "最新ordinanceCode",
        ])
        for name, recs in sorted(matches.items()):
            recs_sorted = sorted(recs, key=lambda r: r["submitDateTime"] or "", reverse=True)
            latest = recs_sorted[0]
            codes = sorted(set(r["edinetCode"] for r in recs if r["edinetCode"]))
            w.writerow([
                name, latest["status"], "/".join(codes), len(recs),
                latest["submitDateTime"], latest["docID"], latest["docDescription"],
                latest["formCode"], latest["ordinanceCode"],
            ])

    unmatched = [e["canonical"] for e in entries if e["canonical"] not in matches]
    with open("edinet_unmatched.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(unmatched))

    with open("edinet_doctype_summary.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ordinanceCode", "formCode", "docTypeCode", "docDescription(先頭20字)", "件数"])
        for (oc, fc, dtc, desc), cnt in sorted(doctype_counter.items(), key=lambda x: -x[1]):
            w.writerow([oc, fc, dtc, desc, cnt])

    print(f"完了: {len(matches)}/{len(entries)}法人が突合できました。")
    print(f"未突合: {len(unmatched)}法人 -> edinet_unmatched.txt")
    print("結果: edinet_reit_matches.csv / edinet_doctype_summary.csv")


def cmd_sample(args):
    if not Path("edinet_reit_matches.csv").exists():
        sys.exit("先に `scan` を実行して edinet_reit_matches.csv を作成してください")
    with open("edinet_reit_matches.csv", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    target = next((r for r in rows if r["REIT名(サイト表記)"] == args.name), None)
    if target is None:
        sys.exit(f"'{args.name}' が edinet_reit_matches.csv に見つかりません")
    doc_id = target["最新docID"]
    print(f"docID={doc_id} ({target['最新docDescription']}) を type=5 (CSV) で取得します...")
    r = api_get(f"documents/{doc_id}", {"type": 5})
    if r is None:
        sys.exit("取得失敗（404）。docIDの閲覧期間が過ぎているか、CSVが提供されていない書類の可能性があります。")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    target_csvs = [n for n in z.namelist() if "XBRL_TO_CSV" in n and n.endswith(".csv") and "jpcrp" in n.lower()]
    if not target_csvs:
        target_csvs = [n for n in z.namelist() if "XBRL_TO_CSV" in n and n.endswith(".csv")]
    if not target_csvs:
        sys.exit(f"CSVが見つかりません。zip内一覧: {z.namelist()[:30]}")

    all_rows = []
    for csvname in target_csvs:
        raw = z.read(csvname)
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError:
            text = raw.decode("utf-16le")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        for row in reader:
            label = row.get("項目名") or ""
            if "資産" in label:
                row["_source_csv"] = csvname
                all_rows.append(row)

    if not all_rows:
        print("「資産」を含む項目名の行が見つかりませんでした。CSVの列名を確認してください:")
        raw = z.read(target_csvs[0])
        text = raw.decode("utf-16", errors="replace")
        print(text.split("\n", 1)[0])
        return

    outname = f"edinet_sample_{args.name}_assets.csv"
    keys = sorted({k for r in all_rows for k in r.keys()})
    with open(outname, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(all_rows)
    print(f"{len(all_rows)}行を {outname} に出力しました。")
    print("「相対年度」が当期(直近)のもの・「連結・個別」・「値」欄を見て、AUMに使う行を確定してください。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("scan", help="REIT名とEDINETコードの突合+書類種別コードの実データ確認")
    p1.add_argument("--days", type=int, default=400, help="過去何日分を走査するか(既定400)")
    p1.add_argument("--sleep", type=float, default=0.4, help="リクエスト間隔(秒、既定0.4)")
    p1.set_defaults(func=cmd_scan)

    p2 = sub.add_parser("sample", help="代表1社の資産関連XBRL項目を一覧表示")
    p2.add_argument("--name", required=True, help="reit_names.jsonにある法人名(サイト表記)")
    p2.set_defaults(func=cmd_sample)

    args = ap.parse_args()
    args.func(args)
