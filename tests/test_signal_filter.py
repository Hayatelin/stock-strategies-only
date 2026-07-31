"""signal_filter 的行為測試。

Why this file exists: the module shipped without tests, while the handover doc
claimed it had been unit tested. Verified 2026-07-31; do not delete.
"""

import pytest

from stock_strategies import signal_filter as sf


WATCHLIST = [
    {"stock_id": "2330", "name": "A", "category": "半導體"},
    {"stock_id": "2454", "name": "B", "category": "IC設計"},
    {"stock_id": "2404", "name": "C", "category": "半導體設備"},
    {"stock_id": "2317", "name": "D", "category": "AI伺服器"},
    {"stock_id": "3017", "name": "E", "category": "散熱"},
    {"stock_id": "2881", "name": "F", "category": "金融"},
    {"stock_id": "2603", "name": "G", "category": "航運"},
    {"stock_id": "0050", "name": "H", "category": "ETF"},
    {"stock_id": "1301", "name": "I", "category": "塑化"},
    {"stock_id": "2207", "name": "J", "category": "汽車"},
    {"stock_id": "1216", "name": "K", "category": "食品"},
    {"stock_id": "9999", "name": "L", "category": "未知產業"},
]


def watch(stock_id, score):
    return {"stock_id": stock_id, "action": "WATCH", "signal_score": score}


def test_coarse_group_maps_semiconductor_chain_together():
    # 這五種細分類是同一條供應鏈，必須落在同一個粗分類，否則去重形同虛設
    groups = {sf.coarse_group(c) for c in ("半導體", "IC設計", "記憶體", "封測", "半導體設備")}
    assert groups == {"半導體鏈"}


def test_coarse_group_passes_through_unknown_category():
    # 新增產業時不能被吃掉，也不能歸零
    assert sf.coarse_group("未知產業") == "未知產業"
    assert sf.coarse_group("") == "其他"
    assert sf.coarse_group(None) == "其他"


def test_same_coarse_group_keeps_highest_score():
    market = {"note": ""}
    signals = [watch("2330", 60), watch("2454", 80), watch("2404", 70)]
    out = sf.apply_display_limits(signals, WATCHLIST, market)
    assert [s["stock_id"] for s in out] == ["2454"]
    assert "省略 2 檔" in market["note"]


def test_different_groups_all_kept_and_note_untouched():
    market = {"note": "原本的大盤註記"}
    signals = [watch("2330", 60), watch("2881", 55), watch("2603", 50)]
    out = sf.apply_display_limits(signals, WATCHLIST, market)
    assert len(out) == 3
    assert market["note"] == "原本的大盤註記"


def test_max_watch_cap_applies_after_dedupe():
    market = {"note": ""}
    signals = [watch("2330", 90), watch("2317", 89), watch("2881", 88),
               watch("2603", 87), watch("0050", 86), watch("1301", 85),
               watch("2207", 84), watch("1216", 83), watch("9999", 82)]
    out = sf.apply_display_limits(signals, WATCHLIST, market, max_watch=3)
    assert len(out) == 3
    assert [s["stock_id"] for s in out] == ["2330", "2317", "2881"]
    assert "超過 3 檔上限省略" in market["note"]


def test_buy_and_skip_are_never_dropped():
    market = {"note": ""}
    signals = [
        {"stock_id": "2330", "action": "BUY", "signal_score": 90},
        {"stock_id": "2454", "action": "BUY", "signal_score": 88},
        {"stock_id": "2404", "action": "SKIP", "signal_score": 30},
        {"stock_id": "2317", "action": "ERROR", "signal_score": 0},
        watch("3017", 60),
        watch("2881", 59),
    ]
    out = sf.apply_display_limits(signals, WATCHLIST, market)
    kept = lambda a: [s for s in out if s["action"] == a]
    assert len(kept("BUY")) == 2
    assert len(kept("SKIP")) == 1
    assert len(kept("ERROR")) == 1


def test_note_reports_hidden_count_so_nothing_disappears_silently():
    market = {"note": ""}
    signals = [watch("2330", 70), watch("2454", 69), watch("2404", 68)]
    sf.apply_display_limits(signals, WATCHLIST, market)
    assert "WATCH 共 3 檔，僅列出 1 檔" in market["note"]
    assert "Signals" in market["note"]


def test_missing_watchlist_collapses_everything_into_one_group():
    """記錄一個尖銳邊界：沒有 watchlist（或 category 欄位空白）時，
    所有 WATCH 的粗分類都會是「其他」，於是只留下最高分那一檔。

    這不是 crash，但如果 Google Sheet 的 category 欄被改名或清空，
    每日報告的 WATCH 會靜靜地縮成 1 檔。故意用測試把這個行為釘住，
    哪天有人想改成「查不到分類就不去重」，會先看到這條測試。
    """
    market = {"note": ""}
    signals = [watch("2330", 70), watch("2454", 69)]
    out = sf.apply_display_limits(signals, None, market)
    assert [s["stock_id"] for s in out] == ["2330"]
    assert "省略 1 檔" in market["note"]  # 至少有講被藏了幾檔，不是完全無聲


def test_missing_market_dict_does_not_crash():
    # 2330 半導體鏈 / 2881 金融，不同粗分類，兩檔都該留下
    signals = [watch("2330", 70), watch("2881", 69)]
    assert len(sf.apply_display_limits(signals, WATCHLIST, None)) == 2


@pytest.mark.parametrize("empty", [[], None])
def test_empty_input_returned_as_is(empty):
    assert sf.apply_display_limits(empty, WATCHLIST, {"note": ""}) == empty


def test_original_list_is_not_mutated():
    market = {"note": ""}
    signals = [watch("2330", 70), watch("2454", 69)]
    before = list(signals)
    sf.apply_display_limits(signals, WATCHLIST, market)
    assert signals == before
