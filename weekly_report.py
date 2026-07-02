"""本週回顧週報（雲端版）。

週日晚間由 GitHub Actions 執行，彙整：
1. 本週（近 7 天）選出的 BUY / WATCH 訊號（讀 Google Sheet「Signals」分頁）
2. 目前持股的本週變化（讀「持股」分頁 + FinMind 現價）
3. 系統成績單（累積訊號成效，讀「Performance」分頁）
推一則 Telegram。純通知，不代下單。

重用 repo 既有模組：sheet / notify / data / performance。
所需環境變數：FINMIND_TOKEN, TELEGRAM_*, GOOGLE_SHEET_ID, GOOGLE_CREDS_JSON。
"""

from datetime import datetime, timedelta

import gspread

from stock_strategies.sheet import get_gsheet, read_latest_signals, read_performance
from stock_strategies.notify import send_telegram
from stock_strategies.data import get_price_history
from stock_strategies.performance import summary as perf_summary

HOLD_SHEET = "持股"


def _to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_date(s):
    s = str(s).strip().replace("/", "-")[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def _week_signals(days: int = 7):
    """近 days 天內的 Signals 紀錄。"""
    rows = read_latest_signals(limit=500)
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for r in rows:
        d = _parse_date(r.get("date", ""))
        if d and d >= cutoff:
            out.append(r)
    return out


def _holdings():
    sh = get_gsheet()
    try:
        ws = sh.worksheet(HOLD_SHEET)
    except gspread.WorksheetNotFound:
        return []
    return [
        r for r in ws.get_all_records()
        if str(r.get("狀態", "")).strip() == "持有"
    ]


def _week_change(sid: str):
    """回 (現價, 相對 5 交易日前漲跌%)。取不到回 (None, None)。"""
    try:
        df = get_price_history(str(sid), years=1)
    except Exception:
        return None, None
    if df is None or df.empty or "close" not in df.columns:
        return None, None
    close = df["close"].dropna().tolist()
    if not close:
        return None, None
    last = float(close[-1])
    ref = float(close[-6]) if len(close) >= 6 else float(close[0])
    wk = (last / ref - 1) * 100 if ref else None
    return last, wk


def main():
    today = datetime.now()
    start = (today - timedelta(days=6)).strftime("%m/%d")
    end = today.strftime("%m/%d")
    msg = [f"🗓️ *本週回顧* ({start} ~ {end})", ""]

    # 1. 本週選股
    sigs = _week_signals(7)
    buys = [s for s in sigs if str(s.get("action", "")).strip() == "BUY"]
    watches = [s for s in sigs if str(s.get("action", "")).strip() == "WATCH"]
    msg.append(f"📊 *本週選股*  BUY {len(buys)} / WATCH {len(watches)}")
    if buys:
        msg.append("🟢 本週 BUY：")
        for s in buys[:15]:
            d = _parse_date(s.get("date", ""))
            ds = d.strftime("%m/%d") if d else ""
            msg.append(
                f"• {ds} {s.get('stock_id','')} {s.get('name','')}"
                f"｜{s.get('signal_score','')}分"
            )
    else:
        msg.append("本週無 BUY 訊號")
    msg.append("")

    # 2. 持股週變化
    holds = _holdings()
    msg.append("💼 *持股週變化*")
    if not holds:
        msg.append("目前無持股")
    else:
        for r in holds:
            sid = str(r.get("代號", "")).strip()
            name = str(r.get("名稱", "")).strip()
            entry = _to_float(r.get("買進價"))
            last, wk = _week_change(sid)
            if last is None:
                msg.append(f"• {sid} {name}｜查價失敗，請手動確認")
                continue
            parts = [f"• {sid} {name}｜現價 {last:.2f}"]
            if wk is not None:
                parts.append(f"本週 {wk:+.1f}%")
            if entry:
                parts.append(f"距買進 {(last / entry - 1) * 100:+.1f}%")
            msg.append("｜".join(parts))
    msg.append("")

    # 3. 系統成績單（累積）
    try:
        stats = perf_summary(read_performance())
        if stats["count"] > 0:
            msg.append("📈 *系統成績單（累積）*")
            msg.append(
                f"已完成 {stats['count']} 筆｜T+20 勝率 {stats['winrate_t20']}%"
                f"｜平均報酬 {stats['avg_t20']}%"
            )
            msg.append("")
    except Exception:
        pass

    msg.append("_僅供參考，實際買賣請自行判斷，本程式不代下單。_")
    send_telegram("\n".join(msg))


if __name__ == "__main__":
    main()
