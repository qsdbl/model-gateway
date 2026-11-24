"""Token 估算（P41）：中英文分别算，别再一律 chars//4。

**为什么必须改**（2026-11-19 线上实测，hy4flash）：

| 文本 | 字符数 | 上游 prompt_tokens | chars//4 的估计 | 偏差 |
|---|---|---|---|---|
| 中文「好。」+「测」×200 | 202 | **223** | 50 | **低估 4.5 倍** |
| 英文 "Answer with one word: ok. " + "test "×70 | 376 | **99** | 94 | 基本准 |
| 中英混合 | 232 | **153** | 58 | 低估 2.6 倍 |

上游用的是中文友好的分词器：**中文一字约 1 个 token，英文约 4 个字符 1 个 token**。
一律除以 4 对中文内容会低估 4 倍。

这件事在 P40 之前一直没被发现，因为 token 估算只用于两件事：
1. 展示用的「token 估算」字段；
2. 上下文窗口校验（`need_context`）。

两处都**低估**了中文请求的体积 → 上下文超限的请求会被放行（真到上游才报错），
而且——这是 P41 的关键——**调度器要用它来估算「本次请求大概要花多少钱」**，
估错了就会把用户送去贵模型。P41 起，估算直接决定钱，必须准。

算法：按 Unicode 区间分类（CJK 全角算 1，其余 ASCII 算 0.25），
再叠加每条消息的固定开销（role / 分隔符，实测约 12 token/条）。
刻意**略微高估**（约 +5%）：估高一点只是提前进贵的模型，估低了会漏计费。
"""
from __future__ import annotations

import unicodedata
from typing import Any

# 每条消息的固定开销（role 标记 + 分隔符）。实测：单条中文 22 字符
# → 22 + 22（"好。"+ 空）…… 保守取 12，实测各长度下都覆盖得住。
MESSAGE_OVERHEAD = 12

# 高估系数：宁可高估（提前避开超长），不可低估（漏计费）。
SAFETY = 1.05


def _is_wide(ch: str) -> bool:
    """是不是「一个字算一个 token」的字符。"""
    if ch < "\x80":
        return False
    # 全角/中文/日文/韩文等：Unicode East Asian Width = Wide 或 Fullwidth
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return True
    # 部分 emoji / 符号也算一个字
    return unicodedata.category(ch) in ("So", "Sk")


def estimate_text(text: str) -> int:
    """估算一段文本的 token 数。"""
    if not text:
        return 0
    wide = 0
    narrow = 0
    for ch in text:
        if _is_wide(ch):
            wide += 1
        else:
            narrow += 1
    # 宽字符 1 token/字；窄字符约 4 字符 1 token
    return int(wide + narrow / 4)


def estimate_messages(messages: Any) -> int:
    """估算一组消息的 prompt token 数（含每条消息的固定开销）。"""
    if not isinstance(messages, list):
        return 0
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        total += MESSAGE_OVERHEAD
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_text(content)
        elif isinstance(content, list):
            # 多模态：content 是一串 {type, text/image_url}
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += estimate_text(str(part.get("text", "")))
                else:
                    # 图片按固定开销算（真实取决于分辨率，这里不猜）
                    total += 85
        if m.get("tool_calls"):
            total += estimate_text(str(m["tool_calls"]))
    return int(total * SAFETY)


def estimate_payload(payload: dict[str, Any]) -> int:
    """估算一次请求的 prompt token 数。"""
    return estimate_messages(payload.get("messages"))
