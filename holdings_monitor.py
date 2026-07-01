"""持股停損停利監控（雲端版）。

每交易日收盤後由 GitHub Actions 執行：
1. 讀 Google 表格「持股」分頁裡狀態為「持有」的每一筆。
2. 用 FinMind 取最新收盤價。
3. 套用「混合式」規則：先固定 -8% 停損；一旦曾漲超過 +10% → 改用移動停利
   （停損線 = 期間最高價 × 0.92，跟著高點往上爬）。
4. 把新的「期間最高價」寫回表格（讓移動停利跨日累積）。
5. 觸及停損／首次鎖利 → 發 Telegram 提醒（只提醒，不代下單）。

重用 repo 現有模組：sheet.get_gsheet / notify.send_telegram / data.get_price_history。
所需環境變數（GitHub Secrets）：FINMIND_TOKEN, TELEGRAM_*, GOOGLE_SHEET_ID, GOOGLE_CREDS_JSON。
"""

from datetime import datetime

import gspread

from stock_strategies.sheet import get_gsheet
from stock_strategies.notify import send_telegram
from stock_strategies.data import get_price_history

HOLD_SHEET = "持股"
STOP_PCT = 0.08   # 停損 8%
TAKE_PCT = 0.10   # 獲利超過 +10% 後切換成移動停利


def _to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def get_last_close(stock_id: str):
    """用 FinMind 取最新收盤價；取不到回 None。"""
    try:
        df = get_price_history(str(stock_id), years=1)
    except Exception as e:  # noqa: BLE001
        print(f"[holdings] {stock_id} 查價例外: {e}")
        return None
    if df is None or df.empty or "close" not in df.columns:
        return None
    close = df["close"].dropna()
    if close.empty:
        return None
    return float(close.iloc[-1])


def main():
    sh = get_gsheet()
    try:
        ws = sh.worksheet(HOLD_SHEET)
    except gspread.WorksheetNotFound:
        send_telegram("⚠️ 找不到『持股』分頁，請先在 Google 表格建立（欄位：代號 名稱 買進日 買進價 股數 期間最高價 狀態）。")
        return

    headers = [h.strip() for h in ws.row_values(1)]
    col = {h: i + 1 for i, h in enumerate(headers)}  # 1-based 欄位位置
    records = ws.get_all_records()

    holdings = [
        (i, r) for i, r in enumerate(records, start=2)
        if str(r.get("狀態", "")).strip() == "持有"
    ]

    today = datetime.now().strftime("%Y/%m/%d")
    if not holdings:
        send_telegram(f"📌 *持股停損停利檢查* {today}\n目前無持股，今日無需檢查。")
        return

    alerts, normals = [], []
    for row_idx, r in holdings:
        sid = str(r.get("代號", "")).strip()
        name = str(r.get("名稱", "")).strip()
        entry = _to_float(r.get("買進價"))
        if not sid or entry is None or entry <= 0:
            continue
        peak = _to_float(r.get("期間最高價")) or entry

        p = get_last_close(sid)
        if p is None:
            normals.append(f"• {sid} {name}｜查價失敗，請手動確認")
            continue

        new_peak = max(peak, p)
        trailing = new_peak >= entry * (1 + TAKE_PCT)
        stop = new_peak * (1 - STOP_PCT) if trailing else entry * (1 - STOP_PCT)
        mode = "移動停利" if trailing else "固定"
        pnl = (p - entry) / entry * 100
        just_locked = trailing and peak < entry * (1 + TAKE_PCT)

        # 寫回新的期間最高價
        if "期間最高價" in col and abs(new_peak - peak) > 1e-9:
            try:
                ws.update_cell(row_idx, col["期間最高價"], round(new_peak, 2))
            except Exception as e:  # noqa: BLE001
                print(f"[holdings] 寫回最高價失敗 {sid}: {e}")

        line = (
            f"{sid} {name}｜現價 {p:.2f}｜損益 {pnl:+.1f}%｜"
            f"停損線 {stop:.2f}（{mode}）"
        )
        if p <= stop:
            alerts.append(f"🔴 觸及停損！{line} → 建議出場")
        elif just_locked:
            alerts.append(f"🟢 已鎖利，改用移動停利、續抱跟漲｜{line}")
        else:
            normals.append(f"• {line}")

    msg = [f"📌 *持股停損停利檢查* {today}", ""]
    if alerts:
        msg.append("⚠️ *需要注意*")
        msg.extend(alerts)
        msg.append("")
    if normals:
        msg.append("持有中：")
        msg.extend(normals)
        msg.append("")
    msg.append("_股價可能為最新或前一交易日收盤，僅供參考；實際買賣請自行判斷，本程式不代下單。_")
    send_telegram("\n".join(msg))


if __name__ == "__main__":
    main()
