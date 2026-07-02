# -*- coding: utf-8 -*-
"""199 檔全量策略回測週報（雲端版）。

每週六早上由 GitHub Actions 執行：
1. 讀 Google 表格 Watchlist 分頁（enabled=TRUE 的股票）
2. FinMind 抓 3 年日線（優先還原價 TaiwanStockPriceAdj，失敗退回原始價+自動分割偵測）
3. 跑 5 策略對打：現行波段 / 移動停利 / 短線動能 / 60MA趨勢 / 0050 Buy&Hold
4. Telegram 推摘要 + 把互動 HTML 戰情報告直接傳到手機

重用 repo 模組：stock_strategies.sheet.get_gsheet / stock_strategies.notify.send_telegram
環境變數（既有 Secrets）：FINMIND_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                          GOOGLE_SHEET_ID, GOOGLE_CREDS_JSON
"""
import json, os, time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from stock_strategies.sheet import get_gsheet
from stock_strategies.notify import send_telegram

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.environ.get("FINMIND_TOKEN", "")
FEE, TAX = 0.001425, 0.003
INIT_CAP, MAX_POS = 1_000_000, 5
YEARS = 3
BENCH = "0050"
WATCH_SHEET = os.environ.get("WATCH_SHEET", "Watchlist")
REPORT = Path("戰情報告.html")

# ── 資料 ──────────────────────────────────────────────

def _req(params):
    for i in range(4):
        try:
            r = requests.get(FINMIND_URL, params=params, timeout=30)
            if r.status_code == 200:
                j = r.json()
                if j.get("status") == 200:
                    return j.get("data", [])
            time.sleep(3 * (i + 1))
        except Exception:
            time.sleep(3 * (i + 1))
    return []

def fetch(sid, start):
    base = dict(data_id=sid, start_date=start, token=TOKEN)
    rows = _req(dict(base, dataset="TaiwanStockPriceAdj"))
    adj = True
    if not rows:
        rows = _req(dict(base, dataset="TaiwanStockPrice"))
        adj = False
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns={"max": "high", "min": "low", "Trading_Volume": "volume"})
    need = {"date", "open", "high", "low", "close"}
    if not need.issubset(df.columns):
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df[(df["close"] > 0) & (df["open"] > 0)]
    df = df.drop_duplicates(subset="date").sort_values("date").set_index("date")
    if not adj:  # 原始價 → 自動偵測分割（台股漲跌限10%，單日|變動|>15%即異常）
        chg = df["close"] / df["close"].shift(1) - 1
        for d in df.index[(chg.abs() > 0.15).fillna(False)]:
            prev = float(df["close"].shift(1).loc[d]); cur = float(df["close"].loc[d])
            ratio = prev / cur if cur else 1
            k = round(ratio)
            if k >= 2 and abs(ratio - k) / k < 0.15:   # 一拆k
                m = df.index < d
                df.loc[m, ["open", "high", "low", "close"]] /= k
    df["ma60"] = df["close"].rolling(60).mean()
    df["hi20"] = df["close"].rolling(20).max()
    df["mom20"] = df["close"].pct_change(20)
    df["signal"] = (df["close"] >= df["hi20"]) & (df["close"] > df["ma60"]) & df["ma60"].notna()
    return df

def load_watchlist():
    sh = get_gsheet()
    try:
        ws = sh.worksheet(WATCH_SHEET)
    except Exception:
        for w in sh.worksheets():
            head = [h.strip().lower() for h in w.row_values(1)]
            if "stock_id" in head:
                ws = w; break
        else:
            raise RuntimeError("找不到 Watchlist 分頁")
    out = {}
    for r in ws.get_all_records():
        sid = str(r.get("stock_id", "")).strip()
        if sid and str(r.get("enabled", "TRUE")).strip().upper() in ("TRUE", "1", "YES", ""):
            out[sid] = str(r.get("name", sid)).strip()
    return out

# ── 引擎（同本機版，訊號日收盤判定、次日開盤成交）──────

class P:
    __slots__ = ("sid","entry_date","entry_px","shares","cost","high","days")
    def __init__(s, sid, d, px, b):
        s.sid, s.entry_date, s.entry_px = sid, d, px
        s.shares, s.cost, s.high, s.days = b*(1-FEE)/px, b, px, 0

def run(data, names, dates, exit_rule, prm):
    cash, pos, pxit, pent = float(INIT_CAP), {}, {}, []
    eq, trades = [], []
    for d in dates:
        for sid, why in list(pxit.items()):
            p = pos.get(sid)
            if p is None: pxit.pop(sid); continue
            df = data[sid]
            if d not in df.index: continue
            px = float(df.at[d, "open"])
            got = p.shares * px * (1 - FEE - TAX); cash += got
            trades.append(dict(sid=sid, name=names.get(sid, sid),
                entry_date=str(p.entry_date.date()), entry_px=round(p.entry_px, 2),
                exit_date=str(d.date()), exit_px=round(px, 2), days=p.days,
                ret=round(got / p.cost - 1, 4), reason=why))
            pos.pop(sid); pxit.pop(sid)
        cur = cash + sum(q.shares * (float(data[s].at[d,"open"]) if d in data[s].index else q.entry_px) for s, q in pos.items())
        for sid in pent:
            if sid in pos or len(pos) >= MAX_POS: continue
            df = data[sid]
            if d not in df.index: continue
            b = min(cur / MAX_POS, cash)
            if b < 10_000: continue
            pos[sid] = P(sid, d, float(df.at[d, "open"]), b); cash -= b
        pent = []
        for sid, p in pos.items():
            df = data[sid]
            if d not in df.index: continue
            row = df.loc[d]; p.days += 1
            p.high = max(p.high, float(row["close"]))
            why = exit_rule(p, row, prm)
            if why: pxit[sid] = why
        if len(pos) + len(pent) < MAX_POS:
            c = [(float(df.at[d,"mom20"]), sid) for sid, df in data.items()
                 if sid != BENCH and sid not in pos and sid not in pxit
                 and d in df.index and bool(df.at[d,"signal"]) and pd.notna(df.at[d,"mom20"])]
            c.sort(reverse=True)
            pent = [sid for _, sid in c[:MAX_POS - len(pos)]]
        mv = sum(q.shares * (float(data[s].at[d,"close"]) if d in data[s].index else q.entry_px) for s, q in pos.items())
        eq.append(cash + mv)
    return np.array(eq), trades

def x_fixed(p, row, m):
    r = float(row["close"]) / p.entry_px - 1
    if r >= m["target"]: return "停利+%d%%" % round(m["target"]*100)
    if r <= -m["stop"]:  return "停損-%d%%" % round(m["stop"]*100)
    if p.days >= m["hold_days"]: return "時間出場%d日" % m["hold_days"]

def x_trail(p, row, m):
    c = float(row["close"])
    if p.high >= p.entry_px * (1 + m["trail_arm"]):
        if c <= p.high * (1 - m["trail"]): return "移動停利"
    elif c <= p.entry_px * (1 - m["stop"]):
        return "停損-%d%%" % round(m["stop"]*100)

def x_ma(p, row, m):
    if pd.notna(row["ma60"]) and float(row["close"]) < float(row["ma60"]): return "跌破60MA"

def bh(data, dates):
    df = data[BENCH].reindex(dates).ffill()
    sh = INIT_CAP * (1 - FEE) / float(df["open"].dropna().iloc[0])
    eq = sh * df["close"].to_numpy(dtype=float)
    eq[np.isnan(eq)] = INIT_CAP
    return eq, []

def stats(eq, trades, dates):
    eq = np.asarray(eq, float)
    yrs = (dates[-1] - dates[0]).days / 365.25
    dl = np.diff(eq) / eq[:-1]
    m = dict(total_return=round(float(eq[-1]/INIT_CAP-1), 4),
             cagr=round(float((eq[-1]/INIT_CAP)**(1/yrs)-1) if yrs > 0 else 0, 4),
             sharpe=round(float(np.mean(dl)/np.std(dl)*np.sqrt(252)) if np.std(dl) > 0 else 0, 2),
             max_drawdown=round(float(((eq-np.maximum.accumulate(eq))/np.maximum.accumulate(eq)).min()), 4),
             final_equity=round(float(eq[-1])), n_trades=len(trades))
    if trades:
        r = np.array([t["ret"] for t in trades]); w, l = r[r > 0], r[r <= 0]
        m.update(win_rate=round(float(len(w)/len(r)), 4),
                 avg_win=round(float(w.mean()), 4) if len(w) else 0,
                 avg_loss=round(float(l.mean()), 4) if len(l) else 0,
                 profit_factor=round(float(w.sum()/abs(l.sum())), 2) if len(l) and l.sum() else None,
                 avg_days=round(float(np.mean([t["days"] for t in trades])), 1))
    return m

# ── 報告與通知 ────────────────────────────────────────

def make_html(dates, res, n_stocks):
    data_js = json.dumps(dict(
        dates=[str(d.date()) for d in dates],
        strategies={k: dict(label=v["label"], color=v["color"],
                            equity=[round(float(x)) for x in v["eq"]],
                            metrics=v["m"]) for k, v in res.items()}), ensure_ascii=False)
    tpl = """<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>台股量化戰情室（199檔全量）</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>body{background:#0b1220;color:#e2e8f0;font-family:"Noto Sans TC",sans-serif;padding:20px;max-width:1100px;margin:auto}
h1{font-size:22px}.sub{color:#8ea0bf;font-size:13px;margin:6px 0 18px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin-bottom:20px}
.card{background:#121b2e;border:1px solid #1f2b45;border-radius:12px;padding:12px 14px}
.card h3{font-size:12px;margin:0 0 6px}.big{font-size:22px;font-weight:700}
.pos{color:#34d399}.neg{color:#f87171}.row{display:flex;justify-content:space-between;font-size:12px;color:#8ea0bf;margin-top:3px}
.panel{background:#121b2e;border:1px solid #1f2b45;border-radius:12px;padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:7px 9px;text-align:right;border-bottom:1px solid #1f2b45}
th{color:#8ea0bf}td:first-child,th:first-child{text-align:left}</style></head><body>
<h1>📊 台股量化戰情室 — 全量週報</h1><div class="sub" id="sub"></div>
<div class="cards" id="cards"></div>
<div class="panel"><h2 style="font-size:15px">權益曲線</h2><canvas id="eq"></canvas></div>
<div class="panel"><h2 style="font-size:15px">指標對照</h2><table id="mt"></table></div>
<div class="sub">訊號日收盤判定、次日開盤成交｜含手續費與證交稅｜不含股息｜研究用途非投資建議</div>
<script>const D=__DATA__;const S=D.strategies,K=Object.keys(S),f=x=>(x*100).toFixed(1)+"%";
document.getElementById("sub").innerHTML=`回測 ${D.dates[0]} ~ ${D.dates[D.dates.length-1]}｜宇宙 __N__ 檔｜最多同時5檔等權重`;
K.forEach(k=>{const m=S[k].metrics;document.getElementById("cards").insertAdjacentHTML("beforeend",
`<div class="card"><h3>${S[k].label}</h3><div class="big ${m.total_return>=0?"pos":"neg"}">${f(m.total_return)}</div>
<div class="row"><span>年化</span><b>${f(m.cagr)}</b></div><div class="row"><span>回撤</span><b>${f(m.max_drawdown)}</b></div>
<div class="row"><span>Sharpe</span><b>${m.sharpe}</b></div></div>`)});
new Chart(document.getElementById("eq"),{type:"line",data:{labels:D.dates,datasets:K.map(k=>({label:S[k].label,
data:S[k].equity,borderColor:S[k].color,borderWidth:1.6,pointRadius:0,tension:.1}))},
options:{interaction:{mode:"index",intersect:false},plugins:{legend:{labels:{color:"#cbd5e1",boxWidth:12}}},
scales:{x:{ticks:{color:"#64748b",maxTicksLimit:8}},y:{ticks:{color:"#64748b",callback:v=>(v/1e4).toFixed(0)+"萬"}}}}});
const C=[["total_return","總報酬"],["cagr","年化"],["sharpe","Sharpe"],["max_drawdown","回撤"],["n_trades","交易數"],
["win_rate","勝率"],["profit_factor","獲利因子"],["avg_days","均持有"]];
let h="<tr><th>策略</th>"+C.map(c=>`<th>${c[1]}</th>`).join("")+"</tr>";
K.forEach(k=>{const m=S[k].metrics;h+=`<tr><td>${S[k].label}</td>`+C.map(c=>{let v=m[c[0]];
if(v==null)return"<td>—</td>";if(["total_return","cagr","max_drawdown","win_rate"].includes(c[0]))v=f(v);
return`<td>${v}</td>`}).join("")+"</tr>"});document.getElementById("mt").innerHTML=h;</script></body></html>"""
    REPORT.write_text(tpl.replace("__DATA__", data_js).replace("__N__", str(n_stocks)), encoding="utf-8")

def tg_send_file(path, caption=""):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN"); chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat: return
    with open(path, "rb") as f:
        requests.post(f"https://api.telegram.org/bot{tok}/sendDocument",
                      data={"chat_id": chat, "caption": caption},
                      files={"document": (path.name, f, "text/html")}, timeout=60)

# ── 主流程 ────────────────────────────────────────────

def main():
    wl = load_watchlist()
    start = (pd.Timestamp.today() - pd.DateOffset(years=YEARS)).strftime("%Y-%m-%d")
    data, fails = {}, []
    for i, sid in enumerate(list(wl) + ([BENCH] if BENCH not in wl else [])):
        df = fetch(sid, start)
        if df is not None and len(df) >= 120:
            data[sid] = df
        else:
            fails.append(sid)
        time.sleep(0.12)
    if BENCH not in data:
        send_telegram("⚠️ 全量回測失敗：抓不到 0050 基準資料"); return
    dates = data[BENCH].index[60:]
    names = dict(wl); names[BENCH] = "元大台灣50"

    cfg = {
        "S1 現行策略(+10/-8/20日)": ("#4f8ef7", lambda: run(data, names, dates, x_fixed, dict(target=.10, stop=.08, hold_days=20))),
        "S2 移動停利(8%)":          ("#2dd4bf", lambda: run(data, names, dates, x_trail, dict(stop=.08, trail=.08, trail_arm=.10))),
        "S3 短線動能(+12/-5/7日)":  ("#f59e0b", lambda: run(data, names, dates, x_fixed, dict(target=.12, stop=.05, hold_days=7))),
        "S4 60MA趨勢跟隨":          ("#a78bfa", lambda: run(data, names, dates, x_ma, {})),
        "S5 0050買進抱緊":          ("#94a3b8", lambda: bh(data, dates)),
    }
    res = {}
    for label, (color, fn) in cfg.items():
        eq, tr = fn()
        res[label] = dict(label=label, color=color, eq=eq, m=stats(eq, tr, dates))
    make_html(dates, res, len(data) - 1)

    rank = sorted(res.items(), key=lambda kv: kv[1]["m"]["total_return"], reverse=True)
    lines = [f"📊 *全量策略回測週報*（{len(data)-1} 檔、近 {YEARS} 年）", ""]
    medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, (label, v) in enumerate(rank):
        m = v["m"]
        lines.append(f"{medal[i]} {label}｜總報酬 {m['total_return']*100:+.0f}%｜年化 {m['cagr']*100:+.1f}%｜回撤 {m['max_drawdown']*100:.0f}%")
    if fails:
        lines += ["", f"（{len(fails)} 檔資料不足已略過）"]
    lines += ["", "完整互動報告見附件 HTML（手機可直接開）", "_研究用途，不構成投資建議_"]
    send_telegram("\n".join(lines))
    tg_send_file(REPORT, "本週戰情報告 📈")

if __name__ == "__main__":
    main()
