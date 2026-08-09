"""设置相关 API：DeepSeek API Key 的查看（不回显）、保存、清除与连接测试。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from .. import config
from ..models.schemas import ApiKeyTestResult, SettingsUpdate, SettingsView

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=SettingsView)
def get_settings() -> SettingsView:
    return SettingsView(
        deepseek_api_key_configured=bool(config.get_api_key()),
        deepseek_model=config.DEEPSEEK_MODEL,
        cloud_consent_confirmed=config.has_cloud_consent(),
    )


@router.put("", response_model=SettingsView)
def update_settings(payload: SettingsUpdate) -> SettingsView:
    config.set_api_key(payload.deepseek_api_key)
    return SettingsView(
        deepseek_api_key_configured=bool(config.get_api_key()),
        deepseek_model=config.DEEPSEEK_MODEL,
    )


@router.delete("", response_model=SettingsView)
def delete_settings() -> SettingsView:
    config.clear_api_key()
    return SettingsView(
        deepseek_api_key_configured=False,
        deepseek_model=config.DEEPSEEK_MODEL,
    )


@router.post("/test", response_model=ApiKeyTestResult)
async def test_connection() -> ApiKeyTestResult:
    api_key = config.get_api_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="尚未配置 API Key")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{config.DEEPSEEK_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            return ApiKeyTestResult(ok=True, message="连接成功，密钥有效")
        return ApiKeyTestResult(
            ok=False, message=f"密钥无效（HTTP {resp.status_code}）"
        )
    except httpx.HTTPError as exc:
        return ApiKeyTestResult(ok=False, message=f"连接失败：{exc.__class__.__name__}")
