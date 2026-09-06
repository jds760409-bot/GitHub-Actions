#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台指期籌碼快訊 自動抓取工具 v2
資料來源：臺灣期貨交易所(TAIFEX)、臺灣證券交易所(TWSE) 公開資料

用法:
    python chips.py                 # 抓最近一個交易日
    python chips.py -d 2026/09/04   # 指定日期
    python chips.py --html          # 另外輸出 HTML 報表
    python chips.py --watch --html  # 等到 15:00 自動輪詢

安裝:
    pip install requests pandas lxml beautifulsoup4
"""

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
import zipfile

import pandas as pd
import requests

BASE = "https://www.taifex.com.tw"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Referer": BASE + "/cht/3/futContractsDate",
                        "Origin": BASE})

OUTDIR = os.path.dirname(os.path.abspath(__file__))
DEBUG = os.environ.get("CHIPS_DEBUG") == "1"


# ---------------------------------------------------------------- 基礎工具
def _post_html(path, data, retry=3):
    url = BASE + path
    last = None
    for i in range(retry):
        try:
            r = SESSION.post(url, data=data, timeout=25)
            r.encoding = "utf-8"
            if r.status_code == 200 and len(r.text) > 500:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = repr(e)
        time.sleep(2 + i * 2)
    raise RuntimeError(f"抓取失敗 {url}: {last}")


def _tables(html, min_rows=3):
    out = []
    for df in pd.read_html(io.StringIO(html)):
        if len(df) < min_rows:
            continue
        df = df.copy()
        df.columns = range(df.shape[1])
        for c in (0, 1, 2):
            if c in df.columns:
                df[c] = df[c].ffill()
        out.append(df)
    return out


def _num(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).replace(",", "").replace("　", "").strip()
    s = s.replace("(", "-").replace(")", "")
    m = re.search(r"-?\d+\.?\d*", s)
    return int(float(m.group())) if m else None


def _find_row(df, *keywords):
    for _, row in df.iterrows():
        joined = " ".join(str(v) for v in row.tolist())
        if all(k in joined for k in keywords):
            return row
    return None


def _dump(df, name, date_str):
    if df is None:
        return
    p = os.path.join(OUTDIR, f"raw_{name}_{date_str.replace('/', '')}.csv")
    try:
        df.to_csv(p, index=False, encoding="utf-8-sig")
    except Exception:
        pass


def last_trading_day(today=None):
    d = today or dt.date.today()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def prev_trading_day(date_str):
    d = dt.datetime.strptime(date_str, "%Y/%m/%d").date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y/%m/%d")


# ------------------------------------------------- 1. 三大法人 期貨
def fetch_inst_futures(date_str):
    html = _post_html("/cht/3/futContractsDate", {
        "queryType": "2", "goDay": "", "doQuery": "1",
        "dateaddcnt": "", "queryDate": date_str, "commodityId": "",
    })
    tbs = _tables(html, min_rows=5)
    if not tbs:
        raise RuntimeError("三大法人期貨：查無資料（尚未公布或非交易日）")
    df = max(tbs, key=len)

    def grab(product, identity):
        row = _find_row(df, product, identity)
        if row is None:
            return None
        return {
            "淨買賣超口數": _num(row.get(7)),
            "多方未平倉": _num(row.get(9)),
            "空方未平倉": _num(row.get(11)),
            "淨未平倉口數": _num(row.get(13)),
        }

    res = {}
    for prod, key in [("臺股期貨", "TX"), ("小型臺指期貨", "MTX"),
                      ("微型臺指期貨", "TMF"), ("電子期貨", "TE"),
                      ("金融期貨", "TF")]:
        res[key] = {i: grab(prod, i) for i in ("自營商", "投信", "外資")}
    return res, df


# ------------------------------------------------- 2. 三大法人 選擇權
def fetch_inst_options(date_str):
    html = _post_html("/cht/3/callsAndPutsDate", {
        "queryType": "2", "goDay": "", "doQuery": "1",
        "dateaddcnt": "", "queryDate": date_str, "commodityId": "TXO",
    })
    tbs = _tables(html, min_rows=5)
    if not tbs:
        raise RuntimeError("三大法人選擇權：查無資料")
    df = max(tbs, key=len)

    def grab(cp, identity):
        row = _find_row(df, cp, identity)
        if row is None:
            return None
        return {"淨買賣超口數": _num(row.get(8)), "淨未平倉口數": _num(row.get(14))}

    return ({"CALL": {i: grab("買權", i) for i in ("自營商", "投信", "外資")},
             "PUT": {i: grab("賣權", i) for i in ("自營商", "投信", "外資")}}, df)


# ------------------------------------------------- 3. 大額交易人
def fetch_large_trader(date_str, contract="TX"):
    """
    表格欄位(0-11):
      0契約 1到期月份 2交易人類別 3買前五口 4買前五% 5買前十口 6買前十%
      7賣前五口 8賣前五% 9賣前十口 10賣前十% 11全市場未沖銷
    """
    html = _post_html("/cht/3/largeTraderFutQry", {
        "queryType": "1", "goDay": "", "doQuery": "1", "dateaddcnt": "",
        "queryDate": date_str, "commodityId": contract, "contractId": contract,
    })
    tbs = _tables(html, min_rows=2)
    if not tbs:
        raise RuntimeError("大額交易人：查無資料")
    df = max(tbs, key=len)

    out, near_done = {}, False
    for _, row in df.iterrows():
        cells = [str(v) for v in row.tolist()]
        joined = " ".join(cells)
        is_all = "所有契約" in joined
        is_spec = "特定法人" in joined
        top10 = _num(row.get(5))
        top10s = _num(row.get(9))
        if top10 is None or top10s is None:
            continue
        scope = "所有契約" if is_all else ("近月" if not near_done else "其他")
        who = "十大特定法人" if is_spec else "十大交易人"
        out.setdefault(scope, {})[who] = {
            "買方前十": top10, "賣方前十": top10s, "淨部位": top10 - top10s,
            "全市場未沖銷": _num(row.get(11)),
        }
        if is_spec and not is_all:
            near_done = True
    return out, df


# ------------------------------------------------- 4. P/C Ratio
def fetch_pc_ratio(date_str):
    html = _post_html("/cht/3/pcRatio", {
        "queryStartDate": date_str, "queryEndDate": date_str,
    })
    tbs = _tables(html, min_rows=1)
    if not tbs:
        return None
    df = max(tbs, key=len)
    row = df.iloc[-1]
    return {
        "賣權成交量": _num(row.get(1)), "買權成交量": _num(row.get(2)),
        "成交量PC比": str(row.get(3)),
        "賣權未平倉": _num(row.get(4)), "買權未平倉": _num(row.get(5)),
        "未平倉PC比": str(row.get(6)),
    }


# ------------------------------------------------- 5. 全市場未平倉 (每日行情)
def fetch_total_oi(date_str, commodity="MTX"):
    """優先用期交所每日下載 ZIP（最穩），失敗再退回查詢頁。"""
    y, m, d = date_str.split("/")
    url = (f"{BASE}/file/taifex/Dailydownload/DailydownloadCSV/"
           f"Daily_{y}_{m}_{d}.zip")
    try:
        r = SESSION.get(url, timeout=40)
        if r.status_code == 200 and r.content[:2] == b"PK":
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            name = zf.namelist()[0]
            raw = zf.read(name)
            df = pd.read_csv(io.BytesIO(raw), encoding="big5", on_bad_lines="skip")
            df.columns = [str(c).strip() for c in df.columns]
            col_c = next(c for c in df.columns if "契約" == c or c.startswith("契約"))
            col_m = next(c for c in df.columns if "到期月份" in c)
            col_oi = next(c for c in df.columns if "未沖銷" in c)
            sub = df[df[col_c].astype(str).str.strip() == commodity]
            sub = sub[~sub[col_m].astype(str).str.contains("/")]   # 排除價差交易
            for c in df.columns:
                if "交易時段" in c:
                    sub = sub[sub[c].astype(str).str.contains("一般")]
                    break
            total = pd.to_numeric(sub[col_oi], errors="coerce").sum()
            if total > 0:
                return int(total)
    except Exception as e:
        if DEBUG:
            print("ZIP 取全市場未平倉失敗:", e)

    try:
        html = _post_html("/cht/3/futDataDown", {
            "down_type": "1", "commodity_idt2": commodity,
            "queryStartDate": date_str, "queryEndDate": date_str,
        })
        tbs = _tables(html, min_rows=1)
        if tbs:
            df = max(tbs, key=len)
            vals = [_num(v) for v in df.iloc[:, -1].tolist()]
            vals = [v for v in vals if v]
            if vals:
                return max(vals)
    except Exception:
        pass
    return None


def retail_ratio(inst_fut, total_oi):
    if not total_oi:
        return None
    legs = [inst_fut["MTX"][i] for i in ("自營商", "投信", "外資")]
    if any(l is None for l in legs):
        return None
    inst_long = sum(l["多方未平倉"] or 0 for l in legs)
    inst_short = sum(l["空方未平倉"] or 0 for l in legs)
    r_long, r_short = total_oi - inst_long, total_oi - inst_short
    return {
        "全市場未平倉": total_oi,
        "散戶多單": r_long, "散戶空單": r_short,
        "散戶淨部位": r_long - r_short,
        "散戶多空比": round((r_long - r_short) / total_oi * 100, 2),
    }


# ------------------------------------------------- 6. 現貨三大法人
def fetch_twse_inst(date_str):
    d = date_str.replace("/", "")
    url = f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={d}&type=day&response=json"
    try:
        js = SESSION.get(url, timeout=20).json()
        if js.get("stat") != "OK":
            return None
        return {row[0].strip(): _num(row[3]) for row in js["data"]}
    except Exception:
        return None


# ------------------------------------------------- 組報表
def build_report(date_str):
    rep = {"日期": date_str}

    inst_fut, fut_df = fetch_inst_futures(date_str)
    rep["期貨"] = inst_fut
    _dump(fut_df, "fut", date_str)

    # 前一交易日，用來算增減
    rep["前日"] = None
    try:
        pd_str = prev_trading_day(date_str)
        prev_fut, _ = fetch_inst_futures(pd_str)
        rep["前日"] = {"日期": pd_str, "期貨": prev_fut}
    except Exception as e:
        if DEBUG:
            print("前日資料失敗:", e)

    try:
        opt, opt_df = fetch_inst_options(date_str)
        rep["選擇權"] = opt
        _dump(opt_df, "opt", date_str)
    except Exception as e:
        rep["選擇權"] = {"error": str(e)}

    try:
        lt, lt_df = fetch_large_trader(date_str)
        rep["大額交易人"] = lt
        _dump(lt_df, "large", date_str)
    except Exception as e:
        rep["大額交易人"] = {"error": str(e)}

    try:
        rep["PCRatio"] = fetch_pc_ratio(date_str)
    except Exception as e:
        rep["PCRatio"] = {"error": str(e)}

    rep["散戶"] = retail_ratio(inst_fut, fetch_total_oi(date_str, "MTX"))
    rep["現貨三大法人"] = fetch_twse_inst(date_str)
    return rep


def delta(rep, prod, who):
    """今日 vs 前日 淨未平倉增減"""
    try:
        now = rep["期貨"][prod][who]["淨未平倉口數"]
        old = rep["前日"]["期貨"][prod][who]["淨未平倉口數"]
        return now - old
    except Exception:
        return None


# ------------------------------------------------- 輸出：終端機
def print_report(rep):
    f = lambda v: "—" if v is None else f"{v:+,}"
    line = "═" * 50
    print(f"\n{line}\n  台指期籌碼快訊  {rep['日期']}\n{line}")

    for prod, title in (("TX", "台指期 TX"), ("MTX", "小台 MTX")):
        print(f"\n【{title}】")
        for who in ("外資", "投信", "自營商"):
            v = rep["期貨"][prod].get(who)
            if v:
                print(f"  {who:<4} 淨未平倉 {f(v['淨未平倉口數']):>10}"
                      f"  (前日增減 {f(delta(rep, prod, who)):>8})"
                      f"  買賣超 {f(v['淨買賣超口數']):>8}")
    if rep.get("散戶"):
        s = rep["散戶"]
        print(f"  散戶   淨部位 {f(s['散戶淨部位']):>10}"
              f"  多空比 {s['散戶多空比']:+.2f}%")

    o = rep.get("選擇權")
    if isinstance(o, dict) and "error" not in o:
        print("\n【選擇權 TXO 未平倉淨口數】")
        for cp in ("CALL", "PUT"):
            for who in ("外資", "自營商"):
                v = o[cp].get(who)
                if v:
                    print(f"  {cp:<5}{who:<4} {f(v['淨未平倉口數']):>10}")

    p = rep.get("PCRatio")
    if isinstance(p, dict) and "error" not in p:
        print(f"\n【P/C Ratio】未平倉比 {p['未平倉PC比']}　成交量比 {p['成交量PC比']}")

    lt = rep.get("大額交易人")
    if isinstance(lt, dict) and "error" not in lt:
        print("\n【大額交易人 TX】")
        for scope in ("近月", "所有契約"):
            for who, v in (lt.get(scope) or {}).items():
                print(f"  {scope:<5}{who:<8} 淨部位 {f(v['淨部位']):>10}")

    sp = rep.get("現貨三大法人")
    if sp:
        print("\n【現貨買賣超（億元）】")
        for k, v in sp.items():
            if v is not None:
                print(f"  {k:<22} {v/1e8:+,.2f}")
    print()


# ------------------------------------------------- 輸出：HTML
def write_html(rep, path):
    def n(v, unit=""):
        if v is None:
            return "<span class=na>—</span>"
        cls = "up" if v > 0 else ("dn" if v < 0 else "")
        return f"<span class='{cls}'>{v:+,}{unit}</span>"

    S = []

    def sec(title):
        S.append(f"<h2>{title}</h2><table>")

    def row(a, b, c=""):
        S.append(f"<tr><td>{a}</td><td class=r>{b}</td><td class=r s>{c}</td></tr>")

    def end():
        S.append("</table>")

    for prod, title in (("TX", "台指期 TX 未平倉淨口數"),
                        ("MTX", "小台 MTX 未平倉淨口數")):
        sec(title)
        for who in ("外資", "投信", "自營商"):
            v = rep["期貨"][prod].get(who)
            if v:
                row(who, n(v["淨未平倉口數"]), "增減 " + n(delta(rep, prod, who)))
        if prod == "MTX" and rep.get("散戶"):
            s = rep["散戶"]
            row("散戶", n(s["散戶淨部位"]), f"多空比 {s['散戶多空比']:+.2f}%")
        end()

    o = rep.get("選擇權")
    if isinstance(o, dict) and "error" not in o:
        sec("選擇權 TXO 未平倉淨口數")
        for cp, lbl in (("CALL", "買權"), ("PUT", "賣權")):
            for who in ("外資", "自營商", "投信"):
                v = o[cp].get(who)
                if v:
                    row(f"{lbl} {who}", n(v["淨未平倉口數"]),
                        "買賣超 " + n(v["淨買賣超口數"]))
        end()

    p = rep.get("PCRatio")
    if isinstance(p, dict) and "error" not in p:
        sec("Put / Call Ratio")
        row("未平倉量比", p["未平倉PC比"])
        row("成交量比", p["成交量PC比"])
        end()

    lt = rep.get("大額交易人")
    if isinstance(lt, dict) and "error" not in lt:
        sec("大額交易人 TX")
        for scope in ("近月", "所有契約"):
            for who, v in (lt.get(scope) or {}).items():
                row(f"{scope} {who}", n(v["淨部位"]))
        end()

    sp = rep.get("現貨三大法人")
    if sp:
        sec("現貨三大法人買賣超（億元）")
        for k, v in sp.items():
            if v is not None:
                val = v / 1e8
                cls = "up" if val > 0 else "dn"
                row(k, f"<span class={cls}>{val:+,.2f}</span>")
        end()

    html = f"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>台指期籌碼快訊 {rep['日期']}</title>
<style>
:root{{--up:#d32f2f;--dn:#2e7d32}}
*{{box-sizing:border-box}}
body{{font-family:-apple-system,"Noto Sans TC","PingFang TC",sans-serif;
 margin:0;padding:16px;background:#fff;color:#1a1a1a;max-width:680px}}
h1{{font-size:19px;margin:0 0 4px;border-bottom:3px solid #c62828;padding-bottom:8px}}
.date{{color:#777;font-size:13px;margin:6px 0 18px}}
h2{{font-size:14px;margin:22px 0 6px;color:#c62828;font-weight:600}}
table{{border-collapse:collapse;width:100%;font-size:15px}}
td{{border-bottom:1px solid #eee;padding:10px 6px}}
.r{{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}}
.s{{font-size:12px;color:#888;font-weight:400;white-space:nowrap}}
.up{{color:var(--up)}} .dn{{color:var(--dn)}} .na{{color:#bbb}}
footer{{margin-top:28px;color:#999;font-size:11px;line-height:1.6}}
</style>
<h1>台指期籌碼快訊</h1>
<div class=date>資料日期 {rep['日期']}　·　更新 {dt.datetime.now():%Y-%m-%d %H:%M}</div>
{''.join(S)}
<footer>資料來源：臺灣期貨交易所、臺灣證券交易所公開資訊。<br>
本報表由程式自動彙整，僅供參考，不構成投資建議。</footer></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)


# ------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="台指期籌碼自動抓取")
    ap.add_argument("-d", "--date", help="查詢日期 YYYY/MM/DD")
    ap.add_argument("--watch", action="store_true", help="等到15:00後輪詢")
    ap.add_argument("--html", action="store_true", help="輸出 HTML 報表")
    args = ap.parse_args()

    date_str = args.date or last_trading_day().strftime("%Y/%m/%d")

    if args.watch:
        target = dt.datetime.combine(dt.date.today(), dt.time(15, 0))
        if dt.datetime.now() < target:
            wait = (target - dt.datetime.now()).total_seconds()
            print(f"等待至 15:00（{wait/60:.0f} 分鐘）…")
            time.sleep(wait)
        for _ in range(30):
            try:
                rep = build_report(date_str)
                break
            except Exception as e:
                print(f"[{dt.datetime.now():%H:%M:%S}] 尚未公布：{e}")
                time.sleep(60)
        else:
            sys.exit("逾時：今日資料未取得")
    else:
        rep = build_report(date_str)

    print_report(rep)

    tag = date_str.replace("/", "")
    jp = os.path.join(OUTDIR, f"chips_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
    print("→ JSON:", jp)

    if args.html:
        hp = os.path.join(OUTDIR, f"chips_{tag}.html")
        write_html(rep, hp)
        print("→ HTML:", hp)


if __name__ == "__main__":
    main()
