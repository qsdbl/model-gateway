"""统一鉴权（P8 修复）：Chat Completions 与 Responses 端点共用。

P8 自测发现 Responses 端点漏掉 API Key 校验，可无 key 直接调用。
把校验逻辑集中到本模块，避免两个端点各写一份导致再次遗漏。
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from .config import settings


def check_api_key(request: Request) -> None:
    """网关 API Key 校验。未配置 key 时仅本地放行（开发模式）。"""
    if not settings.api_key:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {settings.api_key}":
        raise HTTPException(status_code=401, detail="invalid api key")
