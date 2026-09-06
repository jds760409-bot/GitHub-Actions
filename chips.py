#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台指期籌碼快訊 自動抓取工具
------------------------------------------------
資料來源：臺灣期貨交易所(TAIFEX)、臺灣證券交易所(TWSE) 公開資料

用法：
    python chips.py                 # 抓最近一個交易日
    python chips.py -d 2026/09/04   # 抓指定日期
    python chips.py --watch         # 每天 15:00 起自動輪詢，抓到就輸出
    python chips.py --html          # 另外輸出 HTML 報表

安裝：
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

import pandas as pd
import requests

BASE = "https://www.taifex.com.tw"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Referer": BASE + "/cht/3/futContractsDate",
    "Origin": BASE,
})

OUTDIR = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# 基礎工具
# --------------------------------------------------------------------------
def _post_html(path, data, retry=3):
    """POST 期交所查詢頁，回傳 HTML 字串。"""
    url = BASE + path
    last = None
    for i in range(retry):
        try:
            r = SESSION.post(url, data=data, timeout=25)
            r.encoding = "utf-8"
            if r.status_code == 200 and len(r.text) > 500:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:          # 網路瞬斷就重試
            last = repr(e)
        time.sleep(2 + i * 2)
    raise RuntimeError(f"抓取失敗 {url}: {last}")


def _tables(html, min_rows=3):
    """把 HTML 內所有表格讀出來，回傳 DataFrame list（欄位已攤平成 0..n）。"""
    out = []
    for df in pd.read_html(io.StringIO(html)):
        if len(df) < min_rows:
            continue
        df = df.copy()
        df.columns = range(df.shape[1])       # 期交所是多層表頭，直接改用位置索引
        # 商品名稱那欄有 rowspan，讀出來會是 NaN，往下補
        for c in (0, 1, 2):
            if c in df.columns:
                df[c] = df[c].ffill()
        out.append(df)
    return out


def _num(x):
    """把 '1,234' / '-1,234' / '(1,234)' 轉成 int，失敗回 None。"""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).replace(",", "").replace("　", "").strip()
    s = s.replace("(", "-").replace(")", "")
    m = re.search(r"-?\d+\.?\d*", s)
    return int(float(m.group())) if m else None


def _find_row(df, *keywords):
    """在 DataFrame 中找出「同一列同時包含所有關鍵字」的第一列。"""
    for _, row in df.iterrows():
        joined = " ".join(str(v) for v in row.tolist())
        if all(k in joined for k in keywords):
            return row
    return None


def last_trading_day(today=None):
    """回推最近一個工作日（未含國定假日，遇假日程式會自己往前找）。"""
    d = today or dt.date.today()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


# --------------------------------------------------------------------------
# 1. 三大法人 — 區分各期貨契約
# --------------------------------------------------------------------------
def fetch_inst_futures(date_str):
    html = _post_html("/cht/3/futContractsDate", {
        "queryType": "2", "goDay": "", "doQuery": "1",
        "dateaddcnt": "", "queryDate": date_str, "commodityId": "",
    })
    tbs = _tables(html, min_rows=5)
    if not tbs:
        raise RuntimeError("三大法人期貨：查無資料（可能尚未公布或為非交易日）")
    df = max(tbs, key=len)

    # 欄位位置：0序號 1商品 2身分別 3多方交易 5空方交易 7淨額交易
    #           9多方未平倉 11空方未平倉 13淨額未平倉口數
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

    result = {}
    for prod, key in [("臺股期貨", "TX"), ("小型臺指期貨", "MTX"),
                      ("微型臺指期貨", "TMF"), ("電子期貨", "TE"),
                      ("金融期貨", "TF")]:
        result[key] = {
            "自營商": grab(prod, "自營商"),
            "投信": grab(prod, "投信"),
            "外資": grab(prod, "外資"),
        }
    return result, df


# --------------------------------------------------------------------------
# 2. 三大法人 — 選擇權買賣權分計
# --------------------------------------------------------------------------
def fetch_inst_options(date_str):
    html = _post_html("/cht/3/callsAndPutsDate", {
        "queryType": "2", "goDay": "", "doQuery": "1",
        "dateaddcnt": "", "queryDate": date_str, "commodityId": "TXO",
    })
    tbs = _tables(html, min_rows=5)
    if not tbs:
        raise RuntimeError("三大法人選擇權：查無資料")
    df = max(tbs, key=len)

    # 0序號 1商品 2權別 3身分別 4買方交易 6賣方交易 8交易淨額
    # 10買方未平倉 12賣方未平倉 14未平倉淨額口數
    def grab(cp, identity):
        row = _find_row(df, cp, identity)
        if row is None:
            return None
        return {
            "淨買賣超口數": _num(row.get(8)),
            "淨未平倉口數": _num(row.get(14)),
        }

    return {
        "CALL": {i: grab("買權", i) for i in ("自營商", "投信", "外資")},
        "PUT":  {i: grab("賣權", i) for i in ("自營商", "投信", "外資")},
    }, df


# --------------------------------------------------------------------------
# 3. 大額交易人未沖銷部位（十大交易人 / 十大特定法人）
# --------------------------------------------------------------------------
def fetch_large_trader(date_str, contract="TX"):
    html = _post_html("/cht/3/largeTraderFutQry", {
        "queryType": "1", "goDay": "", "doQuery": "1",
        "dateaddcnt": "", "queryDate": date_str, "commodityId": contract,
    })
    tbs = _tables(html, min_rows=2)
    if not tbs:
        raise RuntimeError("大額交易人：查無資料")
    df = max(tbs, key=len)

    def net(row_kw, buy_i, sell_i):
        row = _find_row(df, row_kw)
        if row is None:
            return None
        b, s = _num(row.get(buy_i)), _num(row.get(sell_i))
        return None if b is None or s is None else b - s

    # 表格結構：買方前五大口數/%、前十大口數/%、賣方前五大…、全市場未沖銷
    return {
        "近月_十大交易人淨部位": net("所有契約", 5, 9) or net("到期月份", 5, 9),
        "raw": df,
    }


# --------------------------------------------------------------------------
# 4. Put/Call Ratio
# --------------------------------------------------------------------------
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
        "賣權成交量": _num(row.get(1)),
        "買權成交量": _num(row.get(2)),
        "成交量PC比": _num(row.get(3)),
        "賣權未平倉": _num(row.get(4)),
        "買權未平倉": _num(row.get(5)),
        "未平倉PC比%": str(row.get(6)),
    }


# --------------------------------------------------------------------------
# 5. 小台散戶多空比（自行計算）
#    散戶淨部位 = 全市場多方未平倉 - 三大法人多方未平倉 - (空方同理)
#    散戶多空比 = 散戶淨部位 / 全市場未平倉量
# --------------------------------------------------------------------------
def fetch_total_oi(date_str, commodity="MTX"):
    """從每日行情取得該商品全市場未沖銷契約量。"""
    try:
        html = _post_html("/cht/3/futDataDown", {
            "down_type": "1", "commodity_idt2": commodity,
            "queryStartDate": date_str, "queryEndDate": date_str,
        })
        tbs = _tables(html, min_rows=1)
        if tbs:
            df = max(tbs, key=len)
            for _, row in df.iterrows():
                v = _num(row.iloc[-1])
                if v:
                    return v
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
        "散戶多單": r_long,
        "散戶空單": r_short,
        "散戶淨部位": r_long - r_short,
        "散戶多空比%": round((r_long - r_short) / total_oi * 100, 2),
    }


# --------------------------------------------------------------------------
# 6. 證交所現貨三大法人買賣超
# --------------------------------------------------------------------------
def fetch_twse_inst(date_str):
    d = date_str.replace("/", "")
    url = f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={d}&type=day&response=json"
    try:
        r = SESSION.get(url, timeout=20)
        js = r.json()
        if js.get("stat") != "OK":
            return None
        out = {}
        for row in js["data"]:
            out[row[0].strip()] = _num(row[3])   # 買賣差額
        return out
    except Exception:
        return None


# --------------------------------------------------------------------------
# 組報表
# --------------------------------------------------------------------------
def build_report(date_str):
    rep = {"日期": date_str}

    inst_fut, fut_df = fetch_inst_futures(date_str)
    rep["期貨"] = inst_fut

    try:
        inst_opt, opt_df = fetch_inst_options(date_str)
        rep["選擇權"] = inst_opt
    except Exception as e:
        rep["選擇權"] = {"error": str(e)}

    try:
        rep["大額交易人"] = {k: v for k, v in fetch_large_trader(date_str).items()
                             if k != "raw"}
    except Exception as e:
        rep["大額交易人"] = {"error": str(e)}

    rep["PCRatio"] = fetch_pc_ratio(date_str)
    rep["散戶"] = retail_ratio(inst_fut, fetch_total_oi(date_str, "MTX"))
    rep["現貨三大法人"] = fetch_twse_inst(date_str)

    # 原始表格存 CSV 備查
    fut_df.to_csv(os.path.join(OUTDIR, f"raw_fut_{date_str.replace('/','')}.csv"),
                  index=False, encoding="utf-8-sig")
    return rep


def print_report(rep):
    d = rep["日期"]
    line = "═" * 46
    print(f"\n{line}\n  台指期籌碼快訊  {d}\n{line}")

    def fmt(v):
        return "—" if v is None else f"{v:+,}"

    print("\n【台指期 TX 未平倉淨口數】")
    for who in ("外資", "投信", "自營商"):
        v = rep["期貨"]["TX"].get(who)
        if v:
            print(f"  {who:<4} 淨未平倉 {fmt(v['淨未平倉口數']):>10}"
                  f"   當日買賣超 {fmt(v['淨買賣超口數']):>9}")

    print("\n【小台 MTX】")
    for who in ("外資", "自營商"):
        v = rep["期貨"]["MTX"].get(who)
        if v:
            print(f"  {who:<4} 淨未平倉 {fmt(v['淨未平倉口數']):>10}")
    if rep.get("散戶"):
        s = rep["散戶"]
        print(f"  散戶   淨部位 {fmt(s['散戶淨部位']):>10}"
              f"   多空比 {s['散戶多空比%']:+.2f}%")

    if isinstance(rep.get("選擇權"), dict) and "error" not in rep["選擇權"]:
        print("\n【選擇權 外資未平倉】")
        for cp in ("CALL", "PUT"):
            v = rep["選擇權"][cp].get("外資")
            if v:
                print(f"  {cp:<5} 淨未平倉 {fmt(v['淨未平倉口數']):>10}")

    if rep.get("PCRatio"):
        print(f"\n【P/C Ratio】未平倉比 {rep['PCRatio']['未平倉PC比%']}")

    if rep.get("大額交易人", {}).get("近月_十大交易人淨部位") is not None:
        print(f"\n【十大交易人】淨部位 "
              f"{fmt(rep['大額交易人']['近月_十大交易人淨部位'])}")

    if rep.get("現貨三大法人"):
        print("\n【現貨買賣超（億元）】")
        for k, v in rep["現貨三大法人"].items():
            if v is not None:
                print(f"  {k:<20} {v/1e8:+,.2f}")
    print()


def write_html(rep, path):
    rows = []

    def add(sec, name, val):
        rows.append(f"<tr><td>{sec}</td><td>{name}</td>"
                    f"<td style='text-align:right'>{val}</td></tr>")

    for prod in ("TX", "MTX"):
        for who, v in rep["期貨"][prod].items():
            if v:
                add(prod, who + " 淨未平倉", f"{v['淨未平倉口數']:+,}")
    if rep.get("散戶"):
        add("MTX", "散戶多空比", f"{rep['散戶']['散戶多空比%']:+.2f}%")

    html = f"""<!doctype html><meta charset="utf-8">
<title>台指期籌碼快訊 {rep['日期']}</title>
<style>
 body{{font-family:system-ui,"Noto Sans TC",sans-serif;margin:24px;color:#1a1a1a}}
 h1{{font-size:20px;border-bottom:2px solid #c00;padding-bottom:6px}}
 table{{border-collapse:collapse;width:100%;max-width:620px;font-size:14px}}
 td,th{{border-bottom:1px solid #e5e5e5;padding:8px 10px}}
 tr:hover{{background:#fafafa}}
</style>
<h1>台指期籌碼快訊　{rep['日期']}</h1>
<table>{''.join(rows)}</table>
<p style="color:#888;font-size:12px">資料來源：臺灣期貨交易所、臺灣證券交易所。本報表僅供參考，不構成投資建議。</p>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="台指期籌碼自動抓取")
    ap.add_argument("-d", "--date", help="查詢日期 YYYY/MM/DD")
    ap.add_argument("--watch", action="store_true",
                    help="等到 15:00 後自動輪詢直到抓到當日資料")
    ap.add_argument("--html", action="store_true", help="另外輸出 HTML 報表")
    args = ap.parse_args()

    date_str = args.date or last_trading_day().strftime("%Y/%m/%d")

    if args.watch:
        target = dt.datetime.combine(dt.date.today(), dt.time(15, 0))
        if dt.datetime.now() < target:
            wait = (target - dt.datetime.now()).total_seconds()
            print(f"等待至 15:00（{wait/60:.0f} 分鐘）…")
            time.sleep(wait)
        for attempt in range(30):            # 最多輪詢 30 次 / 每次 60 秒
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
    jpath = os.path.join(OUTDIR, f"chips_{tag}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
    print(f"→ JSON: {jpath}")

    if args.html:
        hpath = os.path.join(OUTDIR, f"chips_{tag}.html")
        write_html(rep, hpath)
        print(f"→ HTML: {hpath}")


if __name__ == "__main__":
    main()
