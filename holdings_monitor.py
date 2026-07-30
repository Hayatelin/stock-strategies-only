"""持股停損停利監控（雲端版，分資產類別風控）。

每交易日收盤後由 GitHub Actions 執行：
1. 讀 Google 表格「持股」分頁裡狀態為「持有」的每一筆。
2. 判斷資產類別（個股／一般ETF／槓桿ETF／反向ETF）——
   優先採用「類別」欄（可省略），否則由代號自動判斷。
3. 用 FinMind 取最新收盤價，套用該類別的混合式規則：
   先固定停損；一旦漲幅達門檻 → 改用移動停利（停損線＝期間最高 × (1-停損%)）。
4. 把新的「期間最高價」寫回表格（讓移動停利跨日累積）。
5. 槓桿／反向為每日重設商品，持有超過門檻交易日數 → 額外推耗損提醒。
6. 觸及停損／首次鎖利／天數提醒 → 發 Telegram（只提醒，不代下單）。

各類別參數集中在 stock_strategies/asset_class.py，要調整改那裡即可。

重用 repo 現有模組：sheet.get_gsheet / notify.send_telegram / data.get_price_history。
所需環境變數（GitHub Secrets）：FINMIND_TOKEN, TELEGRAM_*, GOOGLE_SHEET_ID, GOOGLE_CREDS_JSON。
"""

from datetime import datetime

import gspread
import pandas as pd

from stock_strategies.sheet import get_gsheet
from stock_strategies.notify import send_telegram
from stock_strategies.data import get_price_history
from stock_strategies import asset_class as ac

HOLD_SHEET = "持股"

# 試算表欄名（「類別」為選填，沒有就由代號自動判斷）
COL_ID = "代號"
COL_NAME = "名稱"
COL_DATE = "買進日"
COL_ENTRY = "買進價"
COL_SHARES = "股數"
COL_PEAK = "期間最高價"
COL_STATUS = "狀態"
COL_CLASS = "類別"

# Google 試算表日期序號的基準日
SHEET_EPOCH = pd.Timestamp("1899-12-30")

# 浮點容差：100*(1+0.10) 會等於 110.00000000000001，
# 不加容差時「現價剛好等於門檻」不會觸發移動停利。
EPS = 1e-9


def _to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_date(v):
    """買進日可能是字串（2026-06-24 / 2026/06/24）或試算表日期序號（46197）。"""
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip()
    if s.replace(".", "", 1).isdigit() and float(s) > 20000:
        try:
            return SHEET_EPOCH + pd.Timedelta(days=float(s))
        except Exception:  # noqa: BLE001
            return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return pd.Timestamp(datetime.strptime(s[:10], fmt))
        except ValueError:
            continue
    try:
        return pd.Timestamp(s)
    except Exception:  # noqa: BLE001
        return None


def get_price_frame(stock_id: str):
    """取近一年日K；失敗或無資料回 None。"""
    try:
        df = get_price_history(str(stock_id), years=1)
    except Exception as e:  # noqa: BLE001
        print(f"[holdings] {stock_id} 查價例外: {e}")
        return None
    if df is None or df.empty or "close" not in df.columns:
        return None
    df = df.dropna(subset=["close"])
    if df.empty:
        return None
    return df


def _since_buy_mask(df, buy_date):
    """回傳「買進日（含）之後」的布林遮罩；無法判斷時回 None。"""
    if df is None or buy_date is None or "date" not in df.columns:
        return None
    try:
        dates = pd.to_datetime(df["date"])
    except Exception:  # noqa: BLE001
        return None
    return dates >= buy_date


def held_trading_days(df, buy_date):
    """買進日之後（含當日）有報價的天數＝實際持有交易日數。"""
    mask = _since_buy_mask(df, buy_date)
    if mask is None:
        return None
    return int(mask.sum())


def peak_since_buy(df, buy_date):
    """買進日以來的最高收盤價。

    補登舊單時試算表的「期間最高價」可能只是買進價，
    若不回頭補算，移動停利在「早就大漲過」的部位上會名存實亡。
    價格資料本來就已抓下來，這步不額外耗 API 額度。
    """
    mask = _since_buy_mask(df, buy_date)
    if mask is None or not bool(mask.any()):
        return None
    try:
        return float(df.loc[mask, "close"].max())
    except Exception:  # noqa: BLE001
        return None


def main():
    sh = get_gsheet()
    try:
        ws = sh.worksheet(HOLD_SHEET)
    except gspread.WorksheetNotFound:
        send_telegram(
            "⚠️ 找不到『持股』分頁，請先在 Google 表格建立"
            "（欄位：代號 名稱 買進日 買進價 股數 期間最高價 狀態，可另加「類別」欄）。"
        )
        return

    headers = [h.strip() for h in ws.row_values(1)]
    col = {h: i + 1 for i, h in enumerate(headers)}  # 1-based 欄位位置
    # numericise_ignore=["all"]：不可讓 gspread 把代號轉成數字。
    # 0050 會變 50（FinMind 查無資料）、006208 會變 6208（可能撞到別家真實個股 → 抓錯價還不報錯）。
    records = ws.get_all_records(numericise_ignore=["all"])

    holdings = [
        (i, r) for i, r in enumerate(records, start=2)
        if str(r.get(COL_STATUS, "")).strip() == "持有"
    ]

    today = datetime.now().strftime("%Y/%m/%d")
    if not holdings:
        send_telegram(f"📌 *持股停損停利檢查* {today}\n目前無持股，今日無需檢查。")
        return

    alerts, normals, needs_check = [], [], []

    for row_idx, r in holdings:
        sid = str(r.get(COL_ID, "")).strip()
        name = str(r.get(COL_NAME, "")).strip()
        entry = _to_float(r.get(COL_ENTRY))
        if not sid:
            needs_check.append(
                f"• 第 {row_idx} 列「代號」空白，*這列沒被檢查到*，請確認試算表"
            )
            continue
        if entry is None or entry <= 0:
            # 不可 silent skip：買進價打錯字（如「104.5元」）會讓整檔失去保護
            needs_check.append(
                f"• {sid} {name}｜「買進價」無法解析（讀到 {r.get(COL_ENTRY)!r}），"
                "*這檔沒被檢查到*，請修正試算表"
            )
            continue

        cls = ac.resolve(sid, r.get(COL_CLASS))
        rules = ac.holding_rules(cls)
        stop_pct = rules["stop_pct"]
        take_pct = rules["take_pct"]

        peak = _to_float(r.get(COL_PEAK)) or entry
        shares = _to_float(r.get(COL_SHARES))
        buy_date = _parse_date(r.get(COL_DATE))

        df = get_price_frame(sid)
        if df is None:
            needs_check.append(
                f"• {sid} {name}｜今日查不到價，*這檔沒被停損檢查到*，請手動看盤"
            )
            continue
        p = float(df["close"].iloc[-1])

        # 期間最高價 = max(試算表既有值, 買進日以來實際最高收盤, 今日收盤)
        # 加入「買進日以來實際最高」是為了讓補登的舊單也能正確啟動移動停利
        hist_peak = peak_since_buy(df, buy_date)
        new_peak = max(x for x in (peak, p, hist_peak) if x is not None)

        take_line = entry * (1 + take_pct)
        trailing = new_peak >= take_line - EPS
        stop = new_peak * (1 - stop_pct) if trailing else entry * (1 - stop_pct)
        mode = "移動停利" if trailing else f"固定 −{stop_pct * 100:.0f}%"
        pnl = (p - entry) / entry * 100
        just_locked = trailing and peak < take_line - EPS
        days = held_trading_days(df, buy_date)

        # 寫回新的期間最高價
        if COL_PEAK in col and abs(new_peak - peak) > 1e-9:
            try:
                ws.update_cell(row_idx, col[COL_PEAK], round(new_peak, 2))
            except Exception as e:  # noqa: BLE001
                print(f"[holdings] 寫回最高價失敗 {sid}: {e}")

        bits = [
            f"{sid} {name}",
            ac.LABEL.get(cls, cls),
            f"現價 {p:.2f}",
            f"損益 {pnl:+.1f}%",
        ]
        if shares:
            bits.append(f"約 {(p - entry) * shares:+,.0f} 元")
        # 槓桿商品在停損線後面補「對應指數等效幅度」，讓嚴格度好理解
        stop_desc = f"停損線 {stop:.2f}（{mode}"
        if cls == ac.LEVERAGED and not trailing:
            eq = ac.index_equivalent(cls, stop_pct * 100)
            if eq:
                stop_desc += f"，≈ 指數 −{eq:.1f}%"
        stop_desc += "）"
        bits.append(stop_desc)

        if days is not None:
            bits.append(f"持有 {days} 個交易日")
        line = "｜".join(bits)

        if p <= stop:
            alerts.append(f"🔴 觸及停損！{line} → 建議出場")
        elif just_locked:
            alerts.append(f"🟢 已鎖利，改用移動停利、續抱跟漲｜{line}")
        else:
            normals.append(f"• {line}")

        # 每日重設商品的持有天數提醒
        limit = rules.get("max_hold_days")
        if limit and days is not None and days > limit:
            alerts.append(
                f"⏳ {sid} {name}（{ac.LABEL.get(cls, cls)}）已持有 {days} 個交易日，"
                f"超過建議上限 {limit} 日。此類商品每日重設，長抱有波動耗損，"
                "請確認是否仍要保留這個部位"
            )

    msg = [f"📌 *持股停損停利檢查* {today}", ""]
    if needs_check:
        msg.append("🟠 *需手動確認（以下未納入自動檢查）*")
        msg.extend(needs_check)
        msg.append("")
    if alerts:
        msg.append("⚠️ *需要注意*")
        msg.extend(alerts)
        msg.append("")
    if normals:
        msg.append("持有中：")
        msg.extend(normals)
        msg.append("")
    msg.append(
        "_停損幅度依商品類別而異（個股 −8%／一般ETF −12%／槓桿ETF −15%／反向ETF −8%，"
        "反向的 −8% 約等於大盤漲 8%）；股價為未還原除權息的收盤價，除息日可能出現假跌破，"
        "僅供參考。實際買賣請自行判斷，本程式不代下單。_"
    )
    send_telegram("\n".join(msg))


if __name__ == "__main__":
    main()
