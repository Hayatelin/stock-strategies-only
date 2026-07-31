"""Telegram 發送：自動分段 + 真的送不出去就拋錯。

背景（2026-07-30 實際發生）：
V3 Daily Signal 的個股詳情訊息超過 Telegram 的 4096 字上限，被 API 以
`400 Bad Request: message is too long` 拒收。但原本的 `send_telegram`
只是把錯誤 print 到 stderr，**workflow 仍然回報 Success**，
所以「失敗就推 Telegram」的保護完全沒被觸發 —— 使用者不知道自己少收了一則報告。

這支模組解決兩件事：
1. 送出前先依「行」切成不超過上限的多段，避免一開始就被拒收。
2. 若某一段真的送失敗，收集錯誤並在最後 raise，讓 workflow 失敗
   → 觸發既有的 `if: failure()` 步驟推播告警。至少「沉默」不會再發生。
"""

from __future__ import annotations

import os
import sys

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram 官方單則上限 4096 字元；留邊界給分段標記與 Markdown 收尾。
HARD_LIMIT = 4096
CHUNK_LIMIT = 3800


def split_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """依行切段，盡量不切斷單一行；單行本身就超長時才硬切。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        while len(line) > limit:  # 極端情況：單行超長
            if buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def send(text: str) -> None:
    """送出訊息；過長自動分段。任一段失敗 → 全部送完後 raise。"""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = TELEGRAM_API.format(token=token)

    chunks = split_text(text)
    total = len(chunks)
    errors: list[str] = []

    for i, chunk in enumerate(chunks, start=1):
        body = chunk if total == 1 else f"{chunk}\n\n（{i}/{total}）"
        payload = {"chat_id": chat_id, "text": body, "parse_mode": "Markdown"}
        try:
            r = requests.post(url, json=payload, timeout=15)
        except Exception as e:  # noqa: BLE001
            errors.append(f"第 {i}/{total} 段連線失敗: {e}")
            continue
        if not r.ok:
            # Markdown 解析失敗是常見原因，退一步用純文字重送一次
            payload.pop("parse_mode", None)
            try:
                r2 = requests.post(url, json=payload, timeout=15)
            except Exception as e:  # noqa: BLE001
                errors.append(f"第 {i}/{total} 段重送連線失敗: {e}")
                continue
            if not r2.ok:
                errors.append(f"第 {i}/{total} 段送失敗: {r2.text[:200]}")

    if errors:
        msg = "Telegram 發送失敗（共 %d 段，%d 段失敗）：%s" % (
            total, len(errors), " | ".join(errors)
        )
        print(msg, file=sys.stderr)
        # 拋錯讓 workflow 失敗 → 觸發 if: failure() 的告警步驟。
        # 不可以只 print，否則會重演「訊息沒送到但 workflow 顯示成功」。
        raise RuntimeError(msg)
