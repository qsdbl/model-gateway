"""账号显示名唯一口径（R1.2）。

背景（用户已两次指出同一问题）：「主号」这种内部标签不能直接显示给用户，
要统一换成手机号掩码。此前修过两次——日报一次、前端 DISP_ALIAS 一次——
都是各修各的，第三处（提醒邮件/流向图）漏掉又犯。这次抽成单一模块：

- 底层 label（凭据文件名 / DB 唯一键 / 快照 key）保持原样，绝不改写；
- **所有展示层**（后台页面、邮件、日报、流向图）拿 label 显示前必须过
  `display_label()`；
- 前端没有 Python 环境，`admin/index.html` 的 `DISP_ALIAS` 与这里保持同一
  张表（README §15 有约定：改一处必须同步另一处）。

新增账号时的规则：
- label 本身是 11 位手机号 → 自动掩码，不用登记；
- 其它 label（昵称/主号）→ 在 DISPLAY_ALIAS 登记掩码；没登记就原样显示。
"""
from __future__ import annotations

import os
import re

def _load_alias() -> dict[str, str]:
    """从环境变量加载「昵称→显示名」映射（JSON 对象）。

    例如：GATEWAY_DISPLAY_ALIAS='{"主号":"138****0000","my-nick":"139****1111"}'
    默认空表：没有需要改名的账号时零配置。
    """
    import json
    raw = os.environ.get("GATEWAY_DISPLAY_ALIAS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


# 与 admin/index.html 的 DISP_ALIAS 同一张表（见模块 docstring）
DISPLAY_ALIAS: dict[str, str] = _load_alias()


def display_label(label: str | None) -> str:
    """底层 label → 用户可读的显示名（手机号掩码优先）。"""
    s = str(label or "").strip()
    # 前端唯一键带平台前缀（qoder:<手机号>），先剥掉
    s = re.sub(r"^qoder:", "", s)
    if s in DISPLAY_ALIAS:
        return DISPLAY_ALIAS[s]
    # 11 位大陆手机号 → 自动掩码（新增账号零登记成本）
    if re.fullmatch(r"1\d{10}", s):
        return s[:3] + "****" + s[-4:]
    return s
