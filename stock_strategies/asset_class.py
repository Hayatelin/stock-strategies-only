"""資產類別判斷 + 各類別的風控／評分參數。

問題背景：原本系統是為「個股」設計的（EPS/ROE 基本面門檻 + 固定 −8% 停損），
套到 ETF / 槓桿ETF / 反向ETF 上會失效：

1. ETF 沒有 EPS/ROE → 基本面永遠「未過門檻」→ 永遠出不了 BUY。
2. 固定 −8% 停損對不同商品嚴格度差很多：
   - 一般 ETF（寬基指數）波動約個股一半，−8% 在正常回檔就被掃出場。
   - 槓桿 ETF（正2）是 2 倍，−8% 只等於指數跌 4%，極易觸發。
   - 反向 ETF（反1）是 1 倍反向，−8% 約等於指數漲 8%（對避險部位反而合理）。
3. 槓桿／反向是「每日重設」商品，長抱本身就有波動耗損，需要天數提醒。

台股代號規則（判斷依據）：
- 結尾 L → 槓桿 ETF，例：00631L 元大台灣50正2
- 結尾 R → 反向 ETF，例：00632R 元大台灣50反1
- 以 00 開頭 → 一般 ETF，例：0050、0056、006208、00878
- 其餘 4 位數字 → 個股，例：2330

註：上市個股代號為 4 位且 >= 1000，因此「全為數字且 < 1000」代表前導零在
讀取過程被吃掉（如 0050 被讀成 50），一律視為 ETF。
"""

from __future__ import annotations

# ── 類別代碼 ──
STOCK = "stock"
ETF = "etf"
LEVERAGED = "leveraged"
INVERSE = "inverse"

LABEL = {
    STOCK: "個股",
    ETF: "一般ETF",
    LEVERAGED: "槓桿ETF",
    INVERSE: "反向ETF",
}

# 使用者在「持股」分頁「類別」欄可以手寫的別名 → 標準類別
ALIASES = {
    "個股": STOCK, "股票": STOCK, "stock": STOCK,
    "etf": ETF, "一般etf": ETF, "指數etf": ETF, "原型etf": ETF,
    "槓桿": LEVERAGED, "槓桿etf": LEVERAGED, "正2": LEVERAGED,
    "正二": LEVERAGED, "leveraged": LEVERAGED,
    "反向": INVERSE, "反向etf": INVERSE, "反1": INVERSE,
    "反一": INVERSE, "inverse": INVERSE,
}


def classify(stock_id) -> str:
    """由代號判斷資產類別。無法判斷時保守回 STOCK（沿用最嚴格的原有規則）。"""
    sid = str(stock_id or "").strip().upper()
    if not sid:
        return STOCK
    if sid.endswith("L"):
        return LEVERAGED
    if sid.endswith("R"):
        return INVERSE
    if sid.startswith("00"):
        return ETF
    if sid.isdigit() and int(sid) < 1000:
        # 前導零被吃掉的 ETF 代號（0050 → 50）
        return ETF
    return STOCK


def resolve(stock_id, override=None) -> str:
    """優先採用使用者在試算表填的類別，否則由代號自動判斷。"""
    if override:
        key = str(override).strip().lower()
        if key in ALIASES:
            return ALIASES[key]
    return classify(stock_id)


# ── 持股監控用的風控規則 ──
# stop_pct      : 停損幅度（固定模式 = 買進價 × (1 - stop_pct)）
# take_pct      : 漲幅達此值後切換為移動停利（停損線 = 期間最高 × (1 - stop_pct)）
# max_hold_days : 持有超過這麼多「交易日」就提醒（每日重設商品的耗損風險）；None = 不提醒
# index_note    : 換算成「對應指數」的等效幅度，只用於通知訊息說明
HOLDING_RULES = {
    STOCK: {
        "stop_pct": 0.08, "take_pct": 0.10,
        "max_hold_days": None, "leverage": 1.0,
    },
    ETF: {
        # 寬基指數波動較個股小，−8% 太緊 → 放寬到 −12%
        # take_pct 必須 > stop_pct/(1-stop_pct)=0.1364，否則切換移動停利時
        # 停損線會低於買進成本（見下方 _assert_rules_consistent）
        "stop_pct": 0.12, "take_pct": 0.18,
        "max_hold_days": None, "leverage": 1.0,
    },
    LEVERAGED: {
        # 2 倍商品：−15% ≈ 指數 −7.5%（與個股 −8% 的嚴格度接近）
        # 停利門檻 +20% ≈ 指數 +10%，避免像 +10% 那樣太早鎖利
        "stop_pct": 0.15, "take_pct": 0.20,
        "max_hold_days": 60, "leverage": 2.0,
    },
    INVERSE: {
        # 1 倍反向：−8% ≈ 指數 +8%，大盤明確轉多才出場，對避險部位合理
        # 但反向多為短期避險，用天數提醒為主
        "stop_pct": 0.08, "take_pct": 0.10,
        "max_hold_days": 30, "leverage": 1.0,
    },
}


def _assert_rules_consistent() -> None:
    """不變量：切換移動停利的那一刻，停損線必須高於買進成本。

    否則系統會推「🟢 已鎖利」但停損線其實在成本之下 —— 是假鎖利。
    條件：(1 + take_pct) × (1 - stop_pct) > 1  ⇔  take_pct > stop_pct / (1 - stop_pct)

    這裡故意在 import 時就檢查並拋錯：參數被改壞時要「大聲失敗」
    （workflow 失敗會推 Telegram），而不是靜默算出錯誤的停損線。
    """
    for cls, r in HOLDING_RULES.items():
        s, t = r["stop_pct"], r["take_pct"]
        if (1 + t) * (1 - s) <= 1:
            raise ValueError(
                f"asset_class 參數不一致（{LABEL.get(cls, cls)}）："
                f"take_pct={t} 必須大於 stop_pct/(1-stop_pct)={s / (1 - s):.4f}，"
                "否則切換移動停利時停損線會低於買進價，「已鎖利」訊息會是假的。"
            )


_assert_rules_consistent()


def holding_rules(cls: str) -> dict:
    return HOLDING_RULES.get(cls, HOLDING_RULES[STOCK])


def index_equivalent(cls: str, pct: float) -> float | None:
    """把商品自身的漲跌幅換算成「對應指數」的近似幅度，供通知訊息說明用。
    反向商品回傳反號。只是粗略換算（未計波動耗損），僅供理解嚴格度。"""
    rules = holding_rules(cls)
    lev = rules.get("leverage", 1.0)
    if not lev:
        return None
    val = pct / lev
    return -val if cls == INVERSE else val


# ── 選股評分用的參數覆寫（階段二 evaluate.py 會用到）──
# 只列出要蓋掉 loader._PARAM_DEFAULTS 的鍵
SCORING_OVERRIDES = {
    ETF: {
        # ETF 沒有 EPS/ROE，基本面門檻無意義 → 關掉硬門檻並把權重移給技術面/回測
        "fundamental_pass_required": False,
        "weight_fundamental": 0.0,
        "weight_technical": 0.45,
        "weight_backtest": 0.55,
        "stop_loss": 0.12,
        "target_return": 0.12,
    },
    LEVERAGED: {
        "fundamental_pass_required": False,
        "weight_fundamental": 0.0,
        "weight_technical": 0.45,
        "weight_backtest": 0.55,
        "stop_loss": 0.15,
        "target_return": 0.20,
    },
    INVERSE: {
        "fundamental_pass_required": False,
        "weight_fundamental": 0.0,
        "weight_technical": 0.45,
        "weight_backtest": 0.55,
        "stop_loss": 0.08,
        "target_return": 0.10,
    },
}


def scoring_overrides(cls: str) -> dict:
    return dict(SCORING_OVERRIDES.get(cls, {}))


def backtest_component(winrate, weight_backtest, risk_notes):
    """決定回測項的得分與「實際生效的權重」。

    原本的寫法是 `winrate = bt.get("winrate") or 0.5`，有兩個問題：
    1. `or` 是 falsy 判斷 → **真實勝率 0% 的股票也會被當成 50%**。
    2. 回測完全沒有樣本時，用 50 分「補」進去等於憑空加分
       （個股虛增最多 20 分、ETF 27.5 分，而 BUY 門檻只有 65 分）。

    改成：
    - 有勝率（含 0.0）→ 照實換算成 0~100 分。
    - 沒有樣本（None）→ 回測權重歸零，交給呼叫端重新正規化其餘權重，
      也就是「這檔沒有回測依據，就只用基本面與技術面評分」，而不是給它 50 分。

    回傳 (bt_score, weight_backtest_effective)。
    """
    if winrate is None:
        risk_notes.append("回測無樣本，本次評分已排除回測權重（不以 50% 勝率代入）")
        return 0.0, 0.0
    return float(winrate) * 100.0, weight_backtest


# 回測樣本數低於此值就不發 BUY（仍可 WATCH）。
# 理由：把「無樣本」的回測權重歸零後，分數會變成 100% 由技術面決定，
# 反而讓「沒有回測依據」的標的比「有依據但勝率普通」的標的更容易上 BUY。
# 這條門檻把語意講清楚：沒被歷史驗證過就不進場，但可以放進觀察名單。
MIN_BACKTEST_SAMPLES_FOR_BUY = 8


def backtest_gate(samples, action, risk_notes):
    """樣本不足時把 BUY 降級為 WATCH；其餘動作原樣回傳。"""
    if action != "BUY":
        return action
    n = samples or 0
    if n < MIN_BACKTEST_SAMPLES_FOR_BUY:
        risk_notes.append(
            f"回測樣本僅 {n} 次（低於 {MIN_BACKTEST_SAMPLES_FOR_BUY} 次），"
            "未經足夠歷史驗證，已由 BUY 降為 WATCH"
        )
        return "WATCH"
    return action


def is_daily_reset(cls: str) -> bool:
    """是否為每日重設商品（有波動耗損）。"""
    return cls in (LEVERAGED, INVERSE)


def signal_semantics_note(cls: str) -> str | None:
    """反向商品的技術訊號語意提醒（做多訊號其實描述的是大盤走弱）。"""
    if cls == INVERSE:
        return "反向ETF：技術訊號描述的是「大盤下跌趨勢」，訊號轉強＝大盤走弱，非本身基本面轉好"
    if cls == LEVERAGED:
        return "槓桿ETF：2 倍放大，且為每日重設商品，長抱有波動耗損"
    return None
