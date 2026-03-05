"""
VK-рассылка постов из группы подписчикам.

Аналог platforms/telegram/posting/ — но для VK API.

Ключевые отличия от Telegram:
  - Нет copy_message/forward_message — собираем сообщение вручную
  - Вложения передаются как строки: "photo-123_456", "video-123_789"
  - Нет видеокружков (video_note) — заменяем обычным видео
  - Rate limit VK: ~20 сообщений/сек для сообществ
"""

import logging
import asyncio
import random

from vkbottle import API
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import User

logger = logging.getLogger(__name__)

# VK rate limit: ~20 msg/sec, но лучше 3-5 для надёжности
VK_SEND_DELAY = 0.25  # 4 сообщения в секунду


async def handle_wall_post_new(post: dict, vk_api: API, session_maker):
    """
    Обработка нового поста на стене группы.

    post — dict из VK Callback API wall_post_new:
      {"id": 123, "from_id": -GROUP_ID, "text": "...", "attachments": [...]}
    """
    post_text = post.get("text", "")
    attachments = post.get("attachments", [])
    owner_id = post.get("owner_id") or post.get("from_id")
    post_id = post.get("id")

    # Фильтрация: пропускаем посты с #pro
    if "#pro" in post_text.lower():
        logger.info(f"VK post {post_id}: skipped (#pro)")
        return

    # Собираем строки вложений для VK API
    attachment_strings = _build_attachments(attachments, owner_id)

    # Получаем VK-подписчиков из БД
    async with session_maker() as session:
        result = await session.execute(
            select(User.vk_id).where(
                User.vk_id.isnot(None),
                User.is_active == True,
                User.subscribed_to_author == True,
            )
        )
        vk_users = [row[0] for row in result.all()]

    if not vk_users:
        logger.info(f"VK post {post_id}: no VK subscribers to notify")
        return

    logger.info(f"VK post {post_id}: broadcasting to {len(vk_users)} VK users")

    # Рассылка с rate limiting
    sent = 0
    failed = 0

    for vk_id in vk_users:
        try:
            await vk_api.messages.send(
                peer_id=vk_id,
                message=post_text if post_text else "📢 Новый пост:",
                attachment=",".join(attachment_strings) if attachment_strings else None,
                random_id=random.randint(1, 2**31),
                dont_parse_links=0,
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"VK broadcast to {vk_id} failed: {e}")

        await asyncio.sleep(VK_SEND_DELAY)

    logger.info(f"VK post {post_id}: broadcast done — sent={sent}, failed={failed}")


def _build_attachments(attachments: list, owner_id: int) -> list[str]:
    """
    Конвертирует VK-вложения в строки для messages.send.

    VK attachment format: "type{owner_id}_{media_id}"
    Примеры: "photo-123456_789", "video-123456_101112"
    """
    result = []

    for att in attachments:
        att_type = att.get("type")
        obj = att.get(att_type, {})

        if att_type == "photo":
            result.append(f"photo{obj.get('owner_id')}_{obj.get('id')}")

        elif att_type == "video":
            result.append(f"video{obj.get('owner_id')}_{obj.get('id')}")

        elif att_type == "doc":
            result.append(f"doc{obj.get('owner_id')}_{obj.get('id')}")

        elif att_type == "audio":
            result.append(f"audio{obj.get('owner_id')}_{obj.get('id')}")

        elif att_type == "link":
            # Ссылки не прикрепляются — добавляем в текст
            url = obj.get("url", "")
            if url:
                result.append(f"link: {url}")

        # Другие типы (poll, market и т.д.) — пропускаем

    return result
