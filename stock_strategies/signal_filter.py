"""每日訊號的顯示上限與同產業去重。

背景：
- 觀察清單 199 檔被分成 **48 種** category，長尾嚴重：9 種只有 1 檔、
  17 種只有 1–2 檔；但真正會洗版的半導體鏈（IC設計18 + 半導體8 + 封測6 +
  記憶體5 + 設備4 = 41 檔）卻分屬 5 種不同 category，用細分類去重完全無效。
  → 因此改用「粗分類 8 組」做去重。
- 2026-07-30 實際發生訊息超過 Telegram 4096 字上限被拒收，
  所以限制檔數不只是版面問題，也是避免訊息遺失。

規則（依使用者決定）：
- BUY 不設上限（BUY 很少見，不該漏掉）。
- WATCH：同一粗分類只留分數最高的一檔，再取前 MAX_WATCH 檔。
- 被略過的檔數會寫進大盤註記，讓使用者知道「還有幾檔沒列出」，不會默默消失。
"""

from __future__ import annotations

MAX_WATCH = 10

# 48 種細分類 → 8 種粗分類
COARSE_GROUPS = {
    "半導體鏈": ["半導體", "IC設計", "記憶體", "封測", "半導體設備", "檢測"],
    "電子硬體": [
        "AI伺服器", "電子", "散熱", "PCB載板", "連接器", "網通",
        "被動元件", "電聲", "面板", "光學", "光電", "光通訊",
    ],
    "金融": ["金融"],
    "傳產原物料": ["塑化", "水泥", "鋼鐵", "化工", "造紙", "紡織", "製鞋", "自行車", "傳產"],
    "運輸": ["航運", "航空"],
    "車與工業": ["汽車", "汽車零件", "機器人", "重電", "綠能", "軍工"],
    "內需服務": ["生技", "食品", "零售", "資產", "電信", "營建", "觀光", "電子通路", "保全"],
    "ETF": ["ETF", "槓桿ETF", "反向ETF"],
}

# 反查表：細分類 → 粗分類
_FINE_TO_COARSE = {
    fine: coarse for coarse, fines in COARSE_GROUPS.items() for fine in fines
}


def coarse_group(category) -> str:
    """細分類轉粗分類；查不到就原樣回傳（新增產業時不會被吃掉）。"""
    c = str(category or "").strip()
    return _FINE_TO_COARSE.get(c, c or "其他")


def _category_map(watchlist) -> dict:
    if not watchlist:
        return {}
    return {
        str(w.get("stock_id", "")).strip(): w.get("category", "")
        for w in watchlist
    }


def apply_display_limits(signals, watchlist=None, market=None, max_watch=MAX_WATCH):
    """回傳精簡後的訊號清單；被略過的數量寫進 market["note"]。

    只動 WATCH，BUY / SKIP / ERROR 原樣保留（SKIP 本來就只計數不列名）。
    """
    if not signals:
        return signals

    cat_of = _category_map(watchlist)
    watches = [s for s in signals if str(s.get("action", "")).strip() == "WATCH"]
    others = [s for s in signals if str(s.get("action", "")).strip() != "WATCH"]
    if not watches:
        return signals

    ranked = sorted(watches, key=lambda s: -(s.get("signal_score") or 0))

    # 1) 同粗分類只留最高分
    seen: set[str] = set()
    deduped, dropped_same_group = [], 0
    for s in ranked:
        g = coarse_group(cat_of.get(str(s.get("stock_id", "")).strip(), ""))
        if g in seen:
            dropped_same_group += 1
            continue
        seen.add(g)
        deduped.append(s)

    # 2) 再取前 max_watch 檔
    kept = deduped[:max_watch]
    dropped_over_limit = len(deduped) - len(kept)

    total_hidden = dropped_same_group + dropped_over_limit
    if total_hidden and isinstance(market, dict):
        parts = [f"WATCH 共 {len(watches)} 檔，僅列出 {len(kept)} 檔"]
        if dropped_same_group:
            parts.append(f"同產業取最高分省略 {dropped_same_group} 檔")
        if dropped_over_limit:
            parts.append(f"超過 {max_watch} 檔上限省略 {dropped_over_limit} 檔")
        note = "📄 " + "；".join(parts) + "（完整清單見 Google 試算表 Signals 分頁）"
        market["note"] = f"{market.get('note', '')}\n{note}".strip()

    keep_ids = {id(s) for s in kept}
    return [s for s in signals if s in others or id(s) in keep_ids]
