"""
VK-рассылка постов из сообществ подписчикам.

Два сценария:
  1. handle_author_post — пост из блога мастера → всем VK-юзерам бота
  2. handle_magazine_post — пост из группы магазинов → юзерам по хэштегу
"""

import re
import logging
import asyncio
import random

from vkbottle import API
from sqlalchemy import select

from app.core.db.models import User, Magazine
from app.core.db.config import session_maker

logger = logging.getLogger(__name__)

VK_SEND_DELAY = 0.25


# ============================================================
# 1. АВТОРСКИЕ ПОСТЫ → всем юзерам VK-бота
# ============================================================

async def handle_author_post(post: dict, vk_api: API, sm):
    """Новый пост в блоге мастера → рассылка всем VK-юзерам."""
    post_text = post.get("text", "")
    post_id = post.get("id")
    text_lower = post_text.lower()

    # Фильтр: #lifestyle или #pro — остаётся только в канале
    if "#lifestyle" in text_lower or "#pro" in text_lower:
        logger.info(f"VK author post {post_id}: skipped (#lifestyle/#pro)")
        return

    attachments = post.get("attachments", [])
    owner_id = post.get("owner_id") or post.get("from_id")
    attachment_strings = _build_attachments(attachments, owner_id)
    # Если в посте есть опрос — добавляем ссылку на пост
    has_poll = any(att.get("type") == "poll" for att in attachments)
    if has_poll and owner_id and post_id:
        poll_link = f"\n\n📊 Голосовать: https://vk.com/wall{owner_id}_{post_id}"
        post_text = post_text + poll_link

    # Все VK-юзеры бота
    async with sm() as session:
        result = await session.execute(
            select(User.vk_id).where(
                User.vk_id.isnot(None),
                User.is_active == True,
            )
        )
        vk_users = [row[0] for row in result.all()]

    if not vk_users:
        logger.info(f"VK author post {post_id}: no users")
        return

    logger.info(f"VK author post {post_id}: broadcasting to {len(vk_users)} users")

    sent, failed = await _broadcast(vk_api, vk_users, post_text, attachment_strings)
    logger.info(f"VK author post {post_id}: sent={sent}, failed={failed}")


# ============================================================
# 2. ПОСТЫ МАГАЗИНОВ → юзерам по хэштегу
# ============================================================

async def handle_magazine_post(post: dict, vk_api: API, sm):
    """Новый пост в группе магазинов → рассылка по хэштегу."""
    post_text = post.get("text", "")
    post_id = post.get("id")

    if "#nobot" in post_text.lower():
        logger.info(f"VK magazine post {post_id}: skipped (#nobot)")
        return

    # Ищем любой хэштег в начале поста
    hashtag_match = re.search(r"#\S+", post_text)
    if not hashtag_match:
        logger.warning(f"VK magazine post {post_id}: no hashtag, skipping")
        return

    hashtag = hashtag_match.group(0).lower()

    # Находим магазин
    async with sm() as session:
        result = await session.execute(
            select(Magazine.id).where(Magazine.vk_hashtag == hashtag)
        )
        magazine_id = result.scalar_one_or_none()

    if not magazine_id:
        logger.warning(f"VK magazine post {post_id}: no magazine for {hashtag}")
        return

    # Убираем хэштег из текста
    clean_text = post_text.replace(hashtag_match.group(0), "").strip()

    attachments = post.get("attachments", [])
    owner_id = post.get("owner_id") or post.get("from_id")
    attachment_strings = _build_attachments(attachments, owner_id)

    # Подписчики магазина
    async with sm() as session:
        result = await session.execute(
            select(User.vk_id).where(
                User.vk_id.isnot(None),
                User.is_active == True,
                User.magazine_id == magazine_id,
            )
        )
        vk_users = [row[0] for row in result.all()]

    if not vk_users:
        logger.info(f"VK magazine post {post_id} ({hashtag}): no subscribers")
        return

    logger.info(f"VK magazine post {post_id} ({hashtag}): broadcasting to {len(vk_users)} users")

    sent, failed = await _broadcast(vk_api, vk_users, clean_text, attachment_strings)
    logger.info(f"VK magazine post {post_id} ({hashtag}): sent={sent}, failed={failed}")


# ============================================================
# ОБЩИЕ ФУНКЦИИ
# ============================================================

async def _broadcast(vk_api, vk_users, text, attachment_strings):
    """Рассылка сообщения списку юзеров с rate limiting."""
    sent = 0
    failed = 0

    for vk_id in vk_users:
        try:
            await vk_api.messages.send(
                peer_id=vk_id,
                message=text if text else "📢 Новый пост:",
                attachment=",".join(attachment_strings) if attachment_strings else None,
                random_id=random.randint(1, 2 ** 31),
                dont_parse_links=0,
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"VK broadcast to {vk_id} failed: {e}")
        await asyncio.sleep(VK_SEND_DELAY)

    return sent, failed


def _build_attachments(attachments: list, owner_id: int) -> list[str]:
    """Конвертирует VK-вложения в строки для messages.send."""
    result = []

    for att in attachments:
        att_type = att.get("type")
        obj = att.get(att_type, {})

        if att_type in ("photo", "video", "doc", "audio"):
            result.append(f"{att_type}{obj.get('owner_id')}_{obj.get('id')}")

    return result