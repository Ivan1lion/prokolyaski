"""
Обработчик Callback API от VK.

Обслуживает три группы:
  1. Группа бота (VK_GROUP_ID) — message_new, message_event
  2. Блог мастера (VK_MY_CHANNEL_ID) — wall_post_new → всем юзерам
  3. Магазины (VK_MAG_ID) — wall_post_new → по хэштегу #mag1, #mag2...
"""

import os
import json
import logging
import asyncio
from aiohttp import web

logger = logging.getLogger(__name__)

# Загружаем конфиг всех групп
BOT_GROUP_ID = int(os.getenv("VK_GROUP_ID", 0))
AUTHOR_GROUP_ID = int(os.getenv("VK_MY_CHANNEL_ID", 0))
MAG_GROUP_ID = int(os.getenv("VK_MAG_ID", 0))

# Словарь: group_id → (secret, confirmation_code)
GROUP_CONFIG = {
    BOT_GROUP_ID: (
        os.getenv("VK_SECRET", ""),
        os.getenv("VK_CONFIRMATION_CODE", ""),
    ),
    AUTHOR_GROUP_ID: (
        os.getenv("VK_SECRET_MY_CHANNEL", ""),
        os.getenv("VK_MY_CHANNEL_CONFIRMATION_CODE", ""),
    ),
    MAG_GROUP_ID: (
        os.getenv("VK_SECRET_MAG", ""),
        os.getenv("VK_MAG_CONFIRMATION_CODE", ""),
    ),
}


async def vk_callback_handler(request: web.Request) -> web.Response:
    """Единая точка входа для всех событий VK Callback API."""
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="bad json", status=400)

    event_type = data.get("type")
    group_id = int(data.get("group_id", 0))

    # Проверяем что группа нам известна
    config = GROUP_CONFIG.get(group_id)
    if not config:
        logger.warning(f"VK callback: unknown group_id={group_id}")
        return web.Response(text="unknown group", status=403)

    secret, confirmation_code = config

    # Проверка секретного ключа
    if secret and data.get("secret") != secret:
        logger.warning(f"VK callback: wrong secret for group {group_id}")
        return web.Response(text="forbidden", status=403)

    # === CONFIRMATION ===
    if event_type == "confirmation":
        return web.Response(text=confirmation_code)

    # === События группы БОТА (message_new, message_event) ===
    if group_id == BOT_GROUP_ID:

        if event_type == "message_new":
            from app.platforms.vk.handlers.user_handlers import handle_message_new
            obj = data.get("object", {}).get("message", {})
            if obj:
                vk_api = request.app.get("vk_api")
                sm = request.app.get("session_maker")
                asyncio.create_task(_safe_handle(handle_message_new, obj, vk_api, sm))
            return web.Response(text="ok")

        if event_type == "message_event":
            from app.platforms.vk.handlers.user_handlers import handle_message_event
            obj = data.get("object", {})
            if obj:
                vk_api = request.app.get("vk_api")
                sm = request.app.get("session_maker")
                asyncio.create_task(_safe_handle(handle_message_event, obj, vk_api, sm))
            return web.Response(text="ok")

    # === WALL_POST_NEW — Блог мастера → всем юзерам VK-бота ===
    if event_type == "wall_post_new" and group_id == AUTHOR_GROUP_ID:
        from app.platforms.vk.posting.vk_broadcaster import handle_author_post
        obj = data.get("object", {})
        if obj:
            vk_api = request.app.get("vk_api")
            sm = request.app.get("session_maker")
            asyncio.create_task(_safe_handle(handle_author_post, obj, vk_api, sm))
        return web.Response(text="ok")

    # === WALL_POST_NEW — Магазины → по хэштегу ===
    if event_type == "wall_post_new" and group_id == MAG_GROUP_ID:
        from app.platforms.vk.posting.vk_broadcaster import handle_magazine_post
        obj = data.get("object", {})
        if obj:
            vk_api = request.app.get("vk_api")
            sm = request.app.get("session_maker")
            asyncio.create_task(_safe_handle(handle_magazine_post, obj, vk_api, sm))
        return web.Response(text="ok")

    return web.Response(text="ok")


async def _safe_handle(handler, *args):
    """Обёртка для безопасного вызова хэндлера в create_task."""
    try:
        await handler(*args)
    except Exception as e:
        logger.exception(f"VK handler error: {e}")