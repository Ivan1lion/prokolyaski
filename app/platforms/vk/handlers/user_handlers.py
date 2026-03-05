"""
VK-хэндлеры пользователя — полный порт Telegram-логики.

Ключевые отличия от Telegram:
  - Нет /команд — используем текстовые кнопки (main_menu) и payload
  - Нет HTML-разметки — plain text (VK не парсит HTML в messages.send)
  - Нет callback_query — есть message_event (для inline-кнопок)
  - Нет FSMContext — состояние храним в Redis (vk_state:{vk_id})
  - Нет video_note (кружочки) — шлём ссылки на видео
  - Нет copy_message — контент собираем вручную
"""

import os
import json
import logging
import asyncio
import random
import contextlib
import time

from vkbottle import API
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone

from app.core.db.models import User, Magazine, UserQuizProfile
from app.core.db.config import session_maker
from app.core.db.crud import get_or_create_user_vk
from app.core.openai_assistant.responses_client import ask_responses_api
from app.core.openai_assistant.prompts_config import get_system_prompt, get_marketing_footer
from app.core.services.pay_config import PAYMENTS
from app.core.services.search_service import search_products
from app.core.services.user_service import (
    get_user_cached,
    update_user_requests,
    update_user_flags,
    try_reserve_request,
    refund_request,
)
from app.core.services.payment_service import create_payment_session
from app.core.redis_client import redis_client
from app.core.quiz.quiz_state_service import (
    get_or_create_quiz_profile,
    get_current_step,
    validate_next,
    save_and_next,
    go_back,
)
from app.core.quiz.config_quiz import QUIZ_CONFIG
from app.core.quiz.photo_ids import VK_UPLOADED_PHOTOS

import app.platforms.vk.keyboards as vk_kb

logger = logging.getLogger(__name__)

# Fallback хранилище состояний (если Redis недоступен)
_memory_state: dict = {}
_dedup_cache: set = set()

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
MY_USERNAME = os.getenv("MASTER_USERNAME")
VK_START_POST = os.getenv("VK_START_POST")
VK_GUIDE_POST = os.getenv("VK_GUIDE_POST")
VK_MANUAL_POST = os.getenv("VK_MANUAL_POST")
VK_RULES_POST = os.getenv("VK_RULES_POST")
VK_AI_VIDEO = os.getenv("VK_AI_VIDEO", "")


# ID магазинов для ПЛАТНЫХ пользователей (тот же список что в TG)
TOP_SHOPS_IDS = [2]



async def _get_state(vk_id: int, key: str) -> str | None:
    """Получает состояние: сначала Redis, fallback на память."""
    try:
        val = await redis_client.get(f"vk_{key}:{vk_id}")
        if val:
            return val
    except Exception:
        pass
    entry = _memory_state.get(f"vk_{key}:{vk_id}")
    if entry:
        value, expires = entry
        if time.time() < expires:
            return value
        else:
            _memory_state.pop(f"vk_{key}:{vk_id}", None)
    return None


async def _set_state(vk_id: int, key: str, value: str, ex: int = 300):
    """Сохраняет состояние: сначала Redis, fallback на память с TTL."""
    _memory_state[f"vk_{key}:{vk_id}"] = (value, time.time() + ex)
    try:
        await redis_client.set(f"vk_{key}:{vk_id}", value, ex=ex)
    except Exception:
        pass


async def _del_state(vk_id: int, key: str):
    """Удаляет состояние из обоих хранилищ."""
    _memory_state.pop(f"vk_{key}:{vk_id}", None)
    try:
        await redis_client.delete(f"vk_{key}:{vk_id}")
    except Exception:
        pass


# ============================================================
# MESSAGE_NEW — обработка текстовых сообщений
# ============================================================

async def handle_message_new(message: dict, vk_api: API, sm):
    """Обрабатывает входящее сообщение от VK-пользователя."""
    vk_id = message.get("from_id")
    text = (message.get("text") or "").strip()
    peer_id = message.get("peer_id", vk_id)
    payload = _parse_payload(message)

    if not vk_id or vk_id < 0:
        return  # Сообщения от групп игнорируем

    # Защита от дубликатов (VK может слать одно сообщение повторно)
    msg_id = message.get("id") or message.get("conversation_message_id")
    if msg_id:
        dedup_key = f"vk_dedup:{vk_id}:{msg_id}"
        # Проверяем в памяти (работает всегда, даже без Redis)
        if dedup_key in _dedup_cache:
            return
        _dedup_cache.add(dedup_key)
        # Чистим старые записи если их слишком много
        if len(_dedup_cache) > 10000:
            _dedup_cache.clear()
        # Дублируем в Redis если доступен
        try:
            await redis_client.set(dedup_key, "1", ex=30)
        except Exception:
            pass

    async with sm() as session:
        user = await get_or_create_user_vk(session, vk_id)

        # --- Обработка payload от кнопок (main_menu keyboard) ---
        if payload:
            # Умный переключатель: ищет либо cmd (от наших кнопок), либо command (от системной ВК)
            cmd = payload.get("cmd") or payload.get("command", "")

            # Перехватываем системную кнопку "Начать"
            if cmd == "start":
                logger.info(f"VK START: text={text!r}, payload={payload}, vk_id={vk_id}")
                await _handle_start(vk_id, peer_id, user, session, vk_api)
                return

            # Передаем все остальные кнопки (включая квиз) в маршрутизатор
            await _handle_command(cmd, vk_id, peer_id, user, session, vk_api, sm)
            return

        # --- Проверяем состояние из Redis (FSM-замена) ---
        state = await _get_state(vk_id, "state")

        if state == "waiting_promo":
            await _del_state(vk_id, "state")
            await _handle_promo_code(text, vk_id, peer_id, user, session, vk_api)
            return

        if state == "waiting_stroller_model":
            await _del_state(vk_id, "state")
            await _handle_stroller_model(text, vk_id, peer_id, session, vk_api)
            return

        if state == "waiting_email":
            await _del_state(vk_id, "state")
            await _handle_email_input(text, vk_id, peer_id, session, vk_api)
            return

        if state == "waiting_master_text":
            await _del_state(vk_id, "state")
            await _handle_master_text(text, vk_id, peer_id, vk_api)
            return

        # --- Проверка closed_menu (аналог TG: блокируем свободный текст) ---
        if text and not payload:
            user_cached = await get_user_cached(session, vk_id, platform="vk")
            if user_cached and user_cached.closed_menu_flag:
                # Временное предупреждение — удалится через 1 секунду
                try:
                    result = await vk_api.messages.send(
                        peer_id=peer_id,
                        message="Завершите действие⤴️",
                        random_id=random.randint(1, 2 ** 31),
                    )
                    if result:
                        await asyncio.sleep(1)
                        with contextlib.suppress(Exception):
                            await vk_api.messages.delete(
                                message_ids=[result],
                                delete_for_all=True,
                            )
                except Exception:
                    pass
                return
        # --- Текстовые команды (от кнопок главного меню) ---
        text_lower = text.lower()

        if text_lower in ("начать", "start", "старт"):
            await _handle_start(vk_id, peer_id, user, session, vk_api)
            return

        # Кнопки главного меню
        menu_map = {
            "⁉️ Как подобрать коляску": "guide",
            "💢 Как не сломать коляску": "rules",
            "✅ Как продлить жизнь коляске": "manual",
            "🤖 AI-консультант": "ai_consultant",
            "🧔‍♂️ Блог мастера": "blog",
            "🆘 Помощь": "help",
            "👤 Мой профиль": "config",
            "📍 Магазин колясок": "contacts",
            "📃 Пользовательское соглашение": "offer",
        }

        for btn_text, cmd in menu_map.items():
            if text_lower == btn_text:
                await _handle_command(cmd, vk_id, peer_id, user, session, vk_api, sm)
                return

        # --- Проверяем AI-режим ---
        ai_mode = await _get_state(vk_id, "ai_mode")
        if ai_mode:
            await _handle_ai_message(text, vk_id, peer_id, user, session, vk_api, ai_mode, sm)
            return

        # --- Свободный текст без режима → показываем меню AI ---
        await _handle_no_state_text(text, vk_id, peer_id, user, session, vk_api)


# ============================================================
# MESSAGE_EVENT — обработка нажатий inline-кнопок
# ============================================================

async def handle_message_event(event: dict, vk_api: API, sm):
    """Обрабатывает нажатие Callback-кнопки (message_event)."""
    vk_id = event.get("user_id")
    peer_id = event.get("peer_id", vk_id)
    payload = event.get("payload", {})
    event_id = event.get("event_id")
    conversation_message_id = event.get("conversation_message_id")
    cmd = payload.get("cmd", "")

    if not vk_id:
        return

    # Подтверждаем событие (убирает спиннер с Callback-кнопки)
    with contextlib.suppress(Exception):
        await vk_api.messages.send_message_event_answer(
            event_id=event_id, user_id=vk_id, peer_id=peer_id,
        )

    async with sm() as session:
        user = await get_or_create_user_vk(session, vk_id)
        await _handle_command(cmd, vk_id, peer_id, user, session, vk_api, sm,
                              conversation_message_id=conversation_message_id,
                              event_id=event_id)


# ============================================================
# ЦЕНТРАЛЬНЫЙ РОУТЕР КОМАНД
# ============================================================

async def _handle_command(cmd, vk_id, peer_id, user, session, vk_api, sm=None,
                          conversation_message_id=None, event_id=None):
    """Маршрутизация команд из payload кнопок и текстового меню."""

    # === Старт / Активация ===
    if cmd == "kb_activation":
        await _handle_activation(vk_id, peer_id, vk_api)

    elif cmd == "pay_access":
        await _handle_payment(vk_id, peer_id, "pay_access", session, vk_api)

    elif cmd == "enter_promo":
        await _set_state(vk_id, "state", "waiting_promo", ex=300)
        await _send(vk_api, peer_id, "Введите код активации текстом:")

    # === Оплата ===
    elif cmd in ("pay29", "pay190", "pay950"):
        await _handle_payment(vk_id, peer_id, cmd, session, vk_api)

    elif cmd == "top_up_balance":
        await _send(vk_api, peer_id, "Выберите тариф:", keyboard=vk_kb.pay_kb())

    # === AI ===
    elif cmd == "ai_consultant":
        await _handle_ai_menu(vk_id, peer_id, user, session, vk_api)

    elif cmd == "first_request":
        await _handle_first_auto_request(vk_id, peer_id, user, session, vk_api, sm)

    elif cmd in ("mode_catalog", "mode_info"):
        mode = "catalog" if cmd == "mode_catalog" else "info"
        await _set_state(vk_id, "ai_mode", mode, ex=3600)
        if mode == "catalog":
            await _send(vk_api, peer_id,
                        "👶 Режим: Подбор коляски\n\n"
                        "Опишите, какую коляску вы ищете (например: «Легкая для самолета» "
                        "или «Вездеход для зимы»)")
        else:
            await _send(vk_api, peer_id,
                        "❓ Режим: Вопрос эксперту\n\n"
                        "Задайте любой вопрос (например: «Что лучше: Anex или Tutis?» "
                        "или «Как смазать колеса?»)")

    # === Инфо-команды ===
    elif cmd == "guide":
        await _handle_guide(vk_id, peer_id, user, session, vk_api)

    elif cmd == "rules":
        await _handle_rules(vk_id, peer_id, user, session, vk_api)

    elif cmd == "manual":
        await _handle_manual(vk_id, peer_id, user, session, vk_api)

    elif cmd == "rules_mode":
        await _handle_rules(vk_id, peer_id, user, session, vk_api)

    elif cmd == "next_service":
        await _handle_pamyatka(vk_id, peer_id, vk_api)

    elif cmd == "get_wb_link":
        await _handle_wb_link(vk_id, peer_id, session, vk_api)

    # === Профиль / Настройки ===
    elif cmd == "config":
        await _handle_config(vk_id, peer_id, user, session, vk_api)

    elif cmd == "contacts":
        await _handle_contacts(vk_id, peer_id, session, vk_api)

    elif cmd == "blog":
        await _handle_blog(vk_id, peer_id, session, vk_api)

    elif cmd == "toggle_blog_sub":
        await _handle_toggle_blog_sub(vk_id, peer_id, session, vk_api)

    elif cmd == "help":
        await _handle_help(vk_id, peer_id, vk_api)

    elif cmd == "contact_master":
        await _handle_contact_master(vk_id, peer_id, session, vk_api)

    elif cmd == "promo":
        await _handle_promo(vk_id, peer_id, session, vk_api)

    elif cmd == "email":
        await _set_state(vk_id, "state", "waiting_email", ex=300)
        await _send(vk_api, peer_id,
                    "📧 Укажите ваш Email для получения чеков.\n\n"
                    "Отправьте адрес электронной почты в ответном сообщении:")

    elif cmd == "service":
        await _set_state(vk_id, "state", "waiting_stroller_model", ex=300)
        await _send(vk_api, peer_id,
                    "🛠 Запись на плановое ТО\n\n"
                    "Пожалуйста, напишите марку и модель вашей коляски одним сообщением "
                    "(например: Tutis Uno 3+, Cybex Priam или Anex m/type)")

    elif cmd == "offer":
        await _handle_offer(vk_id, peer_id, vk_api)

    elif cmd == "quiz_restart":
        await _handle_quiz_restart(vk_id, peer_id, session, vk_api)

    elif cmd == "master26":
        await _handle_master_start(vk_id, peer_id, vk_api)

    elif cmd == "mf_start":
        await _set_state(vk_id, "state", "waiting_master_text", ex=300)
        await _send(vk_api, peer_id,
                    "👀 Жду вашу историю!\n\n"
                    "Опишите ситуацию во всех подробностях: что случилось, в чем сомнения "
                    "или чем хотите поделиться.\n\n"
                    "Напишите всё одним сообщением и отправляйте:")

    # === Квиз ===
    elif cmd == "quiz:start":
        await _handle_quiz_start(vk_id, peer_id, session, vk_api, conversation_message_id)

    elif cmd and cmd.startswith("quiz:select:"):
        option = cmd.split(":")[2]
        await _handle_quiz_select(vk_id, peer_id, option, session, vk_api, conversation_message_id)

    elif cmd == "quiz:next":
        await _handle_quiz_next(vk_id, peer_id, session, vk_api, conversation_message_id, event_id)

    elif cmd == "quiz:back":
        await _handle_quiz_back(vk_id, peer_id, session, vk_api, conversation_message_id)

    elif cmd == "quiz:restore":
        await _handle_quiz_start(vk_id, peer_id, session, vk_api, conversation_message_id)

    # === FAQ ===
    elif cmd in ("faq_1", "faq_2", "faq_3", "faq_4"):
        await _handle_faq(cmd, vk_id, peer_id, vk_api)

    elif cmd == "ai_info":
        await _set_state(vk_id, "ai_mode", "info", ex=3600)
        await _send(vk_api, peer_id,
                    "❓ Режим: Вопрос эксперту\n\n"
                    "Я готов отвечать! Задайте любой вопрос по эксплуатации, "
                    "ремонту или сравнению колясок")

    # elif cmd == "to_feed_like":
    #     await session.execute(
    #         update(User).where(User.vk_id == vk_id).values(first_to_feedback="like")
    #     )
    #     await session.commit()
    #     await _send(vk_api, peer_id, "Отлично! Значит все работает в штатном режиме 🤝")
    #
    # elif cmd == "to_feed_dislike":
    #     await session.execute(
    #         update(User).where(User.vk_id == vk_id).values(first_to_feedback="dislike")
    #     )
    #     await session.commit()
    #     await _send(vk_api, peer_id, "Спасибо! Я проверю Вашу запись и подправлю настройки 🤝")

    elif cmd in ("to_feed_like", "to_feed_dislike"):
        feedback_value = "like" if cmd == "to_feed_like" else "dislike"
        await session.execute(
            update(User).where(User.vk_id == vk_id).values(first_to_feedback=feedback_value)
        )
        await session.commit()
        # Удаляем сообщение с видео и кнопками
        if conversation_message_id:
            with contextlib.suppress(Exception):
                await vk_api.messages.delete(
                    peer_id=peer_id,
                    conversation_message_ids=[conversation_message_id],
                    delete_for_all=True,
                )
        await _send(vk_api, peer_id,
                    "Отлично! Значит все работает в штатном режиме 🤝" if feedback_value == "like"
                    else "Спасибо! Я проверю Вашу запись и подправлю настройки 🤝")



# ============================================================
# ОСНОВНЫЕ ФУНКЦИИ
# ============================================================

async def _handle_start(vk_id, peer_id, user, session, vk_api):
    """Приветствие — аналог /start (только видео + кнопка)."""
    await _send(
        vk_api,
        peer_id,
        "",  # Пустая строка, так как текст нам не нужен
        attachment=VK_START_POST,  # Базовый ID + ключ доступа
        keyboard=vk_kb.quiz_start_kb(),
    )


async def _handle_activation(vk_id, peer_id, vk_api):
    """Экран активации: оплата или промокод."""
    await _send(
        vk_api, peer_id,
        "Оплатите полный доступ ко всем разделам за 1900₽\n"
        "(В пакет также включены 50 бесплатных запросов к AI-консультанту)"
        "\n\n🎫 Есть флаер от магазина-партнера? — нажмите «Ввести код активации» для свободного "
        "доступа к моим личным видеорекомендациям и реальным советам: как выбрать и не сломать коляску",
        attachment=VK_UPLOADED_PHOTOS.get("for_pay.jpg"),
        keyboard=vk_kb.activation_kb(),
    )


async def _handle_ai_menu(vk_id, peer_id, user, session, vk_api):
    """Меню AI-консультанта."""
    user_cached = await get_user_cached(session, vk_id, platform="vk")

    if user_cached and user_cached.show_intro_message:
        # Первый раз: видео + текст без баланса
        await update_user_flags(session, vk_id, platform="vk", show_intro_message=False)

        vk_ai_video = os.getenv("VK_AI_VIDEO", "")
        await _send(
            vk_api, peer_id,
            "AI-консультант готов к работе!\n\n"
            "Он умеет подбирать коляски, а также отвечать на любые вопросы по эксплуатации\n\n"
            "👇 Выберите режим работы:\n\n"
            "[Подобрать коляску] - только для поиска (подбора) подходящей для Вас коляски\n\n"
            "[Другой запрос] - для консультаций, решений вопросов по эксплуатации, "
            "анализа и сравнения уже известных Вам моделей колясок",
            attachment=VK_AI_VIDEO,
            keyboard=vk_kb.ai_mode_kb(),
        )
    else:
        # Все последующие: текст с балансом
        result = await session.execute(
            select(User.requests_left).where(User.vk_id == vk_id)
        )
        real_balance = result.scalar_one_or_none() or 0

        if real_balance > 0:
            text = (f"👋 Чтобы я мог помочь, выберите режим работы:\n\n"
            f"[Подобрать коляску] - только для поиска (подбора) подходящей для Вас коляски\n\n"
            f"[Другой запрос] - для консультаций, решений вопросов по эксплуатации, "
            f"анализа и сравнения уже известных Вам моделей колясок\n\n"
            f"Количество запросов\n"
            f"на вашем балансе: [ {real_balance} ]")
            kb = vk_kb.ai_mode_kb()
        else:
            text = (f"👋 Чтобы я мог помочь, выберите режим работы:\n\n"
            f"[Подобрать коляску] - только для поиска (подбора) подходящей для Вас коляски\n\n"
            f"[Другой запрос] - для консультаций, решений вопросов по эксплуатации, "
            f"анализа и сравнения уже известных Вам моделей колясок\n\n"
            f"Количество запросов\n"
            f"на вашем балансе: [ {real_balance} ]")
            kb = vk_kb.ai_mode_with_balance_kb()

        await _send(vk_api, peer_id, text, keyboard=kb)


async def _handle_no_state_text(text, vk_id, peer_id, user, session, vk_api):
    """Юзер пишет текст без выбранного режима → показываем меню."""
    user_cached = await get_user_cached(session, vk_id, platform="vk")

    if user_cached and user_cached.show_intro_message:
        # Первое сообщение: видео + текст без баланса
        await update_user_flags(session, vk_id, platform="vk", show_intro_message=False)

        await _send(
            vk_api, peer_id,
            "AI-консультант готов к работе!\n\n"
            "Он умеет подбирать коляски, а также отвечать на любые вопросы по эксплуатации\n\n"
            "👇 Выберите режим работы:\n\n"
            "[Подобрать коляску] - только для поиска (подбора) подходящей для Вас коляски\n\n"
            "[Другой запрос] - для консультаций, решений вопросов по эксплуатации, "
            "анализа и сравнения уже известных Вам моделей колясок",
            attachment=VK_AI_VIDEO,
            keyboard=vk_kb.ai_mode_kb(),
        )
    else:
        # Все последующие: текст с балансом
        result = await session.execute(
            select(User.requests_left).where(User.vk_id == vk_id)
        )
        real_balance = result.scalar_one_or_none() or 0

        await _send(
            vk_api, peer_id,
            f"👋 Чтобы я мог помочь, выберите режим работы:\n\n"
            f"[Подобрать коляску] - только для поиска (подбора) подходящей для Вас коляски\n\n"
            f"[Другой запрос] - для консультаций, решений вопросов по эксплуатации, "
            f"анализа и сравнения уже известных Вам моделей колясок\n\n"
            f"Количество запросов\n"
            f"на вашем балансе: [ {real_balance} ]",
            keyboard=vk_kb.ai_mode_with_balance_kb(),
        )


# ============================================================
# AI — ОБРАБОТКА СООБЩЕНИЙ
# ============================================================

async def _handle_ai_message(text, vk_id, peer_id, user, session, vk_api, ai_mode, sm):
    """Обработка свободного текстового сообщения → AI."""
    if not text:
        return

    # Проверяем closed_menu
    user_cached = await get_user_cached(session, vk_id, platform="vk")
    if user_cached and user_cached.closed_menu_flag:
        await _send(vk_api, peer_id, "⚠️ Сначала активируйте доступ к боту.")
        return

    # Атомарно резервируем запрос
    reserved = await try_reserve_request(session, vk_id, platform="vk")
    if not reserved:
        await _send(
            vk_api, peer_id,
            "💡 Чтобы я мог выдать точный результат, выберите пакет запросов:",
            keyboard=vk_kb.pay_kb(),
        )
        return

    is_catalog = (ai_mode == "catalog")

    # Индикация
    await _send(vk_api, peer_id, "🔍 Ищу варианты..." if is_catalog else "🤔 Думаю...")

    # Запускаем AI в фоне
    asyncio.create_task(
        _run_ai_task(vk_api, peer_id, vk_id, text, is_catalog, user, sm)
    )


async def _run_ai_task(vk_api, peer_id, vk_id, user_text, is_catalog, user, sm):
    """Фоновая задача AI-ответа."""
    try:
        async with sm() as session:
            user_cached = await get_user_cached(session, vk_id, platform="vk")
            if not user_cached:
                return

            # Данные магазина
            mag_result = await session.execute(
                select(Magazine).where(Magazine.id == user_cached.magazine_id)
            )
            current_magazine = mag_result.scalar_one_or_none()

            # Данные квиза
            quiz_data_str = "Нет данных."
            quiz_json_obj = {}

            quiz_result = await session.execute(
                select(UserQuizProfile)
                .where(UserQuizProfile.user_id == user_cached.id)
                .order_by(UserQuizProfile.id.desc())
                .limit(1)
            )
            quiz_profile = quiz_result.scalar_one_or_none()
            if quiz_profile and quiz_profile.data:
                try:
                    quiz_json_obj = quiz_profile.data if isinstance(quiz_profile.data, dict) else json.loads(quiz_profile.data)
                    quiz_data_str = json.dumps(quiz_json_obj, ensure_ascii=False) if isinstance(quiz_profile.data, dict) else quiz_profile.data
                except Exception:
                    pass

            # Поиск (только catalog mode)
            products_context = ""
            final_shop_url = None

            if is_catalog:
                if current_magazine:
                    feed_url = current_magazine.feed_url
                    if feed_url and "http" in feed_url:
                        products_context = await search_products(
                            user_query=user_text, quiz_json=quiz_json_obj,
                            allowed_magazine_ids=current_magazine.id, top_k=10)
                    elif feed_url == "PREMIUM_AGGREGATOR":
                        products_context = await search_products(
                            user_query=user_text, quiz_json=quiz_json_obj,
                            allowed_magazine_ids=TOP_SHOPS_IDS, top_k=10)
                    else:
                        final_shop_url = current_magazine.url_website
                else:
                    products_context = await search_products(
                        user_query=user_text, quiz_json=quiz_json_obj,
                        allowed_magazine_ids=TOP_SHOPS_IDS, top_k=10)

            # Генерация ответа
            mode_key = "catalog_mode" if is_catalog else "info_mode"
            system_prompt = get_system_prompt(
                mode=mode_key, quiz_data=quiz_data_str,
                shop_url=final_shop_url, products_context=products_context)

            answer = await ask_responses_api(
                user_message=user_text, system_instruction=system_prompt,
                use_google_search=not bool(products_context),
                allow_fallback=bool(products_context) or not is_catalog)

            # Футеры + снятие флагов
            if is_catalog and user_cached.first_catalog_request:
                answer += get_marketing_footer("catalog_mode")
                await update_user_flags(session, vk_id, platform="vk",
                                        closed_menu_flag=False, first_catalog_request=False)
            elif not is_catalog and user_cached.first_info_request:
                answer += get_marketing_footer("info_mode")
                await update_user_flags(session, vk_id, platform="vk", first_info_request=False)


            # Убираем HTML-теги для VK
            answer = _strip_html(answer)
            await _send(vk_api, peer_id, answer)

            # Если это был первый каталожный запрос — снимаем closed_menu и показываем меню
            if is_catalog and user_cached.first_catalog_request:
                await _send(vk_api, peer_id,
                            "Чтобы свернуть 📋 Меню, нажмите на квадратик с 4 точками 👇",
                            keyboard=vk_kb.main_menu_kb()
                            )

    except Exception as e:
        logger.error(f"AI task error for VK:{vk_id}: {e}", exc_info=True)
        await refund_request(vk_id, platform="vk")
        await _send(vk_api, peer_id, "⚠️ Произошла ошибка. Запрос не списан, попробуйте ещё раз.")


async def _handle_first_auto_request(vk_id, peer_id, user, session, vk_api, sm):
    """Автоматический первый запрос «Подобрать коляску»."""
    reserved = await try_reserve_request(session, vk_id, platform="vk")
    if not reserved:
        await _send(
            vk_api, peer_id,
            "💡 Чтобы завершить анализ, выберите пакет запросов:",
            keyboard=vk_kb.pay_kb(),
        )
        return

    await _send(vk_api, peer_id, "🔍 Анализирую ваши ответы из квиза и ищу лучшее решение...")

    asyncio.create_task(
        _run_ai_task(vk_api, peer_id, vk_id, "Подбери мне подходящую коляску", True, user, sm)
    )


# ============================================================
# ИНФО-КОМАНДЫ
# ============================================================

async def _handle_guide(vk_id, peer_id, user, session, vk_api):
    """Аналог /guide."""
    text = (f"📝 Шпаргалка: «Что нужно учитывать при подборе»"
            f"\n\n1. Основа:"
            f"\n\n• Тип коляски (от рождения или прогулка)"
            f"\n• Функционал (2в1, 3в1 или просто люлька)"
            f"\n• Формат использования (для прогулок или путешествий)"
            f"\n• Сезон (зима или лето)"
            f"\n• Тип дорог (грунт, асфальт или бездорожье)"
            f"\n\n👆 Эти вопросы мы закрыли в самом начале, когда Вы проходили квиз-опрос. Это база (фундамент) "
            f"для поиска и подбора подходящей коляски"
            f"\n\n2. Жизненные нюансы (на этом часто «спотыкаются»):"
            f"\nНегативное влияние этих деталей вы также можете почувствовать на себе уже после покупки, если "
            f"не учтете их сейчас"
            f"\n\n• Ширина лифта (возьмите рулетку и замерьте двери. Если коляска окажется шире проема "
            f"всего на 1 см — будете носить её и ребенка на руках)"
            f"\n• Глубина багажника Вашего авто (критически важен тип складывания и компактность)"
            f"\n• Ваш рост (высокие родители часто пинают ногами ось задних колес у компактных колясок, "
            f"для высоких нужна рама с вынесенной назад осью или телескопическая ручка)"
            f"\n• Этажность и наличие лифта (5-й этаж без лифта = мама после кесарева не поднимет коляску весом 16 кг)"
            f"\n• Эргономика (глубина раскрытия капора, угол откидывания спинки, складывание одной рукой, "
            f"комплектация и т.д.)"
            f"\n• Бюджет (не нужно брать кредиты на коляску - всегда есть альтернатива с сопоставимыми характеристиками)"
            f"\n• Дизайн и цвет (внешний вид коляски должен радовать маму 😍)"
            f"\n\n💡 Это всё необходимо держать в голове при подборе идеального варианта"
            f"\n\nВы также можете просто написать свои условия, предпочтения или жизненные обстоятельства "
            f"🤖AI-консультанту и он сам отфильтрует варианты, которые вам подходят по габаритам или цене"
            f"\n\nНапишите ему как есть, например: "
            f"«Живу на 5 этаже без лифта, узкие двери, муж высокий, бюджет 40к»"
            )
    # Видео из сообщества + текст + кнопки
    await _send(vk_api, peer_id, text,
                attachment=VK_GUIDE_POST,
                keyboard=vk_kb.guide_kb())


async def _handle_rules(vk_id, peer_id, user, session, vk_api):
    """Аналог /rules."""
    await _send(vk_api, peer_id,
                "",  # Пустая строка, так как текст нам не нужен
                attachment=VK_RULES_POST,
                )


async def _handle_manual(vk_id, peer_id, user, session, vk_api):
    """Аналог /manual."""
    await _send(vk_api,
                peer_id,
                "",  # Пустая строка, так как текст нам не нужен
                attachment=VK_MANUAL_POST,
                keyboard=vk_kb.next_service_kb()
                )


async def _handle_pamyatka(vk_id, peer_id, vk_api):
    """Памятка — аналог next_service callback."""
    text = (
        "📌 Памятка: 3 способа как не убить коляску"
        "\n\n🚿 Никакого душа"
        "\nНе мойте колеса из шланга или в ванной. Вода вымоет смазку и подшипники сгниют "
        "за месяц. Только влажная тряпка"
        "\n\n🏋️ Осторожнее с ручкой"
        "\nНе давите на неё всем весом перед бордюром — всегда помогайте ногой, "
        "наступая на заднюю ось. Иначе разболтаете механизм складывания (а это самый дорогой ремонт)"
        "\n\n🛢 Забудьте про WD-40"
        "\nВэдэшка 'сушит' подшипники, а любые бытовые масла работают как магнит для песка — через неделю "
        "механизмы захрустят еще сильнее. Металл и пластик колясок смазывают только силиконом"
        "\n\nСмазку, которой я пользуюсь в мастерской, обычно покупаю у своего поставщика запчастей и прочих "
        "расходников. На валдберриз такую же не нашел, но нашел с такими же характеристиками, соотношение газа к "
        "масляному раствору отличное и по цене норм"
        # "\n\n<a href='https://www.wildberries.ru/catalog/191623733/detail.aspx?targetUrl=MI'>Смазка силиконовая "
        # "для колясок https://www.wildberries.ru/catalog/191623733/detail.aspx?targetUrl=MI</a>"
        "\n\nЕсли смазывать только коляску, то флакона хватит на пару лет"
        "\n\n👆 Если что памятка хранится в разделе [👤 Мой профиль]"
    )
    await _send(vk_api, peer_id, text, keyboard=vk_kb.pamyatka_kb())


async def _handle_wb_link(vk_id, peer_id, session, vk_api):
    """Аналог get_wb_link callback."""
    from sqlalchemy.sql import func
    from sqlalchemy import update as sa_update

    # Аналитика
    stmt = select(User.wb_clicked_at).where(User.vk_id == vk_id)
    clicked_at = (await session.execute(stmt)).scalar_one_or_none()
    if clicked_at is None:
        await session.execute(
            sa_update(User).where(User.vk_id == vk_id).values(wb_clicked_at=func.now())
        )
        await session.commit()

    await _send(vk_api, peer_id,
                "Смазка силиконовая для колясок:\n\n"
                "https://www.wildberries.ru/catalog/274474180/detail.aspx",
                attachment="photo-236264711_456239076")



# ============================================================
# ПРОФИЛЬ / НАСТРОЙКИ
# ============================================================

async def _handle_config(vk_id, peer_id, user, session, vk_api):
    """Аналог /config."""
    text = (
        "👤 Мой профиль\n\n"
        "Выберите действие:"
    )
    await _send(vk_api, peer_id, text, keyboard=vk_kb.config_kb())


async def _handle_contacts(vk_id, peer_id, session, vk_api):
    """Аналог /contacts."""
    result = await session.execute(
        select(Magazine)
        .join(User, User.magazine_id == Magazine.id)
        .where(User.vk_id == vk_id)
    )
    magazine = result.scalar_one_or_none()

    if not magazine:
        await _send(vk_api, peer_id, "Магазин не найден")
        return

    if magazine.name == "[Babykea]":
        await _send(vk_api, peer_id,
                    "🏆 Магазины с высокой репутацией\n\n"
                    "• Первая-Коляска.РФ\n• Boan Baby\n• Lapsi\n• Кенгуру\n• Piccolo")
        return

    parts = [f"{magazine.name}\n",
             f"📍 Город: {magazine.city}",
             f"🏠 Адрес: {magazine.address}"]
    if magazine.url_website:
        parts.append(f"🌐 Сайт: {magazine.url_website}")
    if magazine.vk_magazine:
        parts.append(f"💬 ВКонтакте: {magazine.vk_magazine}")

    text = "\n".join(parts)
    kb = vk_kb.magazine_map_kb(magazine.map_url) if magazine.map_url else None
    await _send(vk_api, peer_id, text, keyboard=kb)


async def _handle_blog(vk_id, peer_id, session, vk_api):
    """Аналог /blog."""
    text = (
        "📝 Блог мастера\n\n"
        "Мой канал: https://t.me/Ivan_PROkolyaski\n\n"
        "#мысливслух — информация к размышлению молодым родителям\n"
        "#маркетинговыеТефтели — маркетинговые уловки производителей колясок\n\n"
        "Подписывайтесь, чтобы не пропустить новые разборы и рекомендации"
    )
    await _send(vk_api, peer_id, text, keyboard=vk_kb.blog_kb())


async def _handle_toggle_blog_sub(vk_id, peer_id, session, vk_api):
    """Переключение подписки на рассылку."""
    stmt = select(User.subscribed_to_author).where(User.vk_id == vk_id)
    is_sub = (await session.execute(stmt)).scalar_one_or_none()
    if is_sub is None:
        is_sub = True

    new_status = not is_sub
    await session.execute(
        update(User).where(User.vk_id == vk_id).values(subscribed_to_author=new_status)
    )
    await session.commit()

    if new_status:
        await _send(vk_api, peer_id, "✅ Рассылка включена! Новые посты будут приходить в этот чат.")
    else:
        await _send(vk_api, peer_id, "🔕 Рассылка отключена. Технические напоминания сохранятся.")


async def _handle_help(vk_id, peer_id, vk_api):
    """Аналог /help."""
    text = (
        "🆘 Проблемы и решения\n\n"
        "1. Ответы на частые вопросы (нажмите кнопку):\n\n"
        "2. Умный помощник — AI-консультант с обширной базой знаний\n\n"
        "3. Связь с мастером — если бот не справился"
    )
    await _send(vk_api, peer_id, text, keyboard=vk_kb.help_kb())


async def _handle_faq(faq_cmd, vk_id, peer_id, vk_api):
    """FAQ видео-ответы."""
    faq_texts = {
        "faq_1": "«Новая коляска скрипит! Мне продали брак?»\n\nВ большинстве случаев скрип — это нормально для новых механизмов. Смажьте шарниры силиконовой смазкой.",
        "faq_2": "«Как снять колеса»\n\nЗависит от модели. Обычно нужно нажать кнопку на оси и потянуть колесо на себя.",
        "faq_3": "«Почему в люльке голова ниже ног?»\n\nПроверьте регулировку дна люльки. У большинства колясок есть регулятор наклона.",
        "faq_4": "«До скольки атмосфер качать колеса?»\n\nОбычно 1.5-2 атм. Точное значение указано на боковине покрышки.",
    }
    text = faq_texts.get(faq_cmd, "Информация недоступна")
    await _send(vk_api, peer_id, f"📹 {text}")


async def _handle_contact_master(vk_id, peer_id, session, vk_api):
    """Связь с мастером."""
    from app.core.db.models import Payment
    result = await session.execute(
        select(Payment).where(
            Payment.telegram_id == vk_id,  # TODO: изменить на vk_id lookup
            Payment.status == "succeeded"
        ).limit(1)
    )
    has_payment = result.scalar_one_or_none()

    if not has_payment:
        await _send(vk_api, peer_id,
                    "Лично отвечаю только на то, что не осилил AI-консультант.\n"
                    "Сначала спросите AI — в 90% случаев этого хватает.")
        return

    await _send(vk_api, peer_id,
                f"✅ Пришлите мне короткое видео (5-10 сек) и опишите суть вопроса.\n\n"
                f"Пишите мне в Telegram: @{MY_USERNAME}")


async def _handle_promo(vk_id, peer_id, session, vk_api):
    """Аналог /promo."""
    stmt = (
        select(Magazine.promo_code, Magazine.is_promo_active)
        .select_from(User)
        .join(Magazine)
        .where(User.vk_id == vk_id)
    )
    result = await session.execute(stmt)
    row = result.one_or_none()

    if not row:
        await _send(vk_api, peer_id, "Сначала активируйте доступ к боту")
        return

    mag_promo, is_active = row
    if not is_active:
        await _send(vk_api, peer_id, "Срок действия вашего промокода истек")
        return

    bot_link = "https://t.me/prokolyaski_bot"
    if mag_promo == "[BABYKEA_PREMIUM]":
        share_promo = "BKEA-4K7X"
        text = (f"👑 У вас PREMIUM-доступ!\n\n"
                f"Гостевой промокод для подруги: {share_promo}\n\n"
                f"Бот: {bot_link}")
    else:
        text = f"Ваш код активации: {mag_promo}\n\nМожете поделиться с друзьями!\n\nБот: {bot_link}"

    await _send(vk_api, peer_id, text)


async def _handle_offer(vk_id, peer_id, vk_api):
    """Аналог /offer."""
    await _send(vk_api, peer_id,
                "1. Публичная оферта:\n"
                "https://telegra.ph/PUBLICHNAYA-OFERTA-na-predostavlenie-prava-ispolzovaniya-"
                "funkcionala-Telegram-bota-Babykea-Bot-i-informacionnyh-materialov-02-23\n\n"
                "2. Политика Конфиденциальности:\n"
                "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-polzovatelej-Telegram-bota-Babykea-"
                "Bot-02-23")


# ============================================================
# ОПЛАТА
# ============================================================

async def _handle_payment(vk_id, peer_id, payment_type, session, vk_api):
    """Создание платёжной сессии через лендинг (VK всегда через лендинг)."""
    cfg = PAYMENTS.get(payment_type)
    if not cfg:
        await _send(vk_api, peer_id, "❌ Неизвестный тариф")
        return

    ps = await create_payment_session(
        session=session, vk_id=vk_id,
        payment_type=payment_type, platform="vk",
    )
    if not ps:
        await _send(vk_api, peer_id, "❌ Ошибка создания платежа. Попробуйте позже.")
        return

    checkout_url = f"{WEBHOOK_HOST}/checkout/{ps.token}"
    text = f"{cfg['description']}\nСумма: {cfg['amount']} ₽"
    await _send(vk_api, peer_id, text, keyboard=vk_kb.payment_button_kb(checkout_url))


# ============================================================
# ПРОМОКОД
# ============================================================

async def _handle_promo_code(code, vk_id, peer_id, user, session, vk_api):
    """Обработка ввода промокода."""
    code = code.strip().upper()

    result = await session.execute(
        select(Magazine).where(Magazine.promo_code == code)
    )
    magazine = result.scalar_one_or_none()

    if not magazine:
        await _send(vk_api, peer_id,
                    "⚠️ Код не сработал"
                    "\n\nВозможно была допущена ошибка. Попробуйте ещё раз"
                    "\n\nЕсли опять не получится напишите мне @Master_PROkolyaski. Я лично проверю "
                    "ваш промокод и открою доступ к видео и советам вручную, чтобы вы могли продолжить без лишних нервов"
                    )
        await _set_state(vk_id, "state", "waiting_promo", ex=300)
        return

    if not magazine.is_promo_active:
        await _send(vk_api, peer_id, "У данного промокода истек срок активации.")
        return

    # Привязываем магазин
    user.promo_code = code
    user.magazine_id = magazine.id

    # Определяем branch
    quiz_result = await session.execute(
        select(UserQuizProfile.branch)
        .where(UserQuizProfile.user_id == user.id)
        .order_by(UserQuizProfile.id.desc())
        .limit(1)
    )
    branch = quiz_result.scalar_one_or_none()

    if branch == "service_only":
        user.closed_menu_flag = False

    await session.commit()
    await redis_client.delete(f"user:vk:{vk_id}")

    # Формируем ответ
    mag_name = magazine.name
    raw_url = magazine.name_website

    if mag_name and mag_name != "[Babykea]":

        # 1. Выделяем имя: делаем заглавными (upper) и берем в кавычки
        name_highlighted = f"«{mag_name.upper()}»"

        # 2. Формируем красивый блок с сайтом на новой строке (plain-text)
        if raw_url:
            clean_url = raw_url.strip()
            if not clean_url.startswith("http"):
                clean_url = f"https://{clean_url}"
            magazine_display = f"{name_highlighted}\nСайт: {clean_url}"
        else:
            magazine_display = name_highlighted

        # 3. Собираем итоговый текст с правильными отступами
        success_text = (
            "✅ Проведена успешная активация по промокоду магазина детских колясок:\n\n"
            f"{magazine_display}\n\n"
            "Контакты продавца будут находиться в меню в разделе\n"
            "[📍 Магазин колясок]\n\n"
            "Теперь проверим бота в деле 👇"
        )
    else:
        success_text = ("✅ Код принят! Добро пожаловать"
                        "\n\nВы сделали верный шаг, чтобы сэкономить нервы при выборе коляски и уберечь свою "
                        "от дорогого ремонта в будущем"
                        "\n\nДавайте проверим бота в деле 👇")

    if branch == "service_only":
        await _send(vk_api, peer_id, success_text, keyboard=vk_kb.rules_mode_kb())
        # service_only — closed_menu уже снят, показываем меню
        await _send(vk_api, peer_id,
                    "Чтобы свернуть 📋 Меню, нажмите на квадратик с 4 точками 👇",
                    keyboard=vk_kb.main_menu_kb()
                    )
    else:
        await _send(vk_api, peer_id, success_text, keyboard=vk_kb.first_request_kb())


# ============================================================
# КВИЗ — с Callback-кнопками и messages.edit (как в Telegram)
# ============================================================

def _get_quiz_photo_vk(step: dict, selected: str | None = None) -> str | None:
    """Получает VK photo attachment для шага квиза."""
    if selected:
        option = step["options"].get(selected)
        if option and "preview" in option:
            return option["preview"].get("photo_vk")
    return step.get("photo_vk")


def _get_quiz_text_vk(step: dict, selected: str | None = None) -> str:
    """Получает текст для VK (plain text, без HTML)."""
    if selected:
        option = step["options"].get(selected)
        if option and "preview" in option:
            return option["preview"].get("text_vk") or _strip_html(option["preview"].get("text", ""))
    return step.get("text_vk") or _strip_html(step.get("text", ""))


async def _handle_quiz_start(vk_id, peer_id, session, vk_api, cmid=None):
    """Старт/рестарт квиза — отправляем НОВОЕ сообщение."""
    user = await get_or_create_user_vk(session, vk_id)
    profile = await get_or_create_quiz_profile(session, user)

    # Сбрасываем прогресс
    profile.branch = None
    profile.current_level = 1
    profile.completed = False
    profile.completed_once = False
    profile.data = {}
    session.add(profile)
    await session.commit()

    # Удаляем предыдущее сообщение с кнопкой
    if cmid:
        with contextlib.suppress(Exception):
            await vk_api.messages.delete(
                peer_id=peer_id,
                conversation_message_ids=[cmid],
                delete_for_all=True,
            )

    # Отправляем первый шаг квиза НОВЫМ сообщением
    await _render_quiz_step_vk(vk_api, peer_id, profile, send_new=True)


async def _handle_quiz_select(vk_id, peer_id, option, session, vk_api, cmid=None):
    """Выбор варианта — РЕДАКТИРУЕМ текущее сообщение."""
    user = await get_or_create_user_vk(session, vk_id)
    profile = await get_or_create_quiz_profile(session, user)

    profile.data["_selected"] = option
    session.add(profile)
    await session.commit()

    # Редактируем сообщение — меняем текст и кнопки
    await _render_quiz_step_vk(vk_api, peer_id, profile, selected=option, cmid=cmid, session=session)


async def _handle_quiz_next(vk_id, peer_id, session, vk_api, cmid=None, event_id=None):
    """Кнопка «Далее» — переход на следующий шаг."""
    user = await get_or_create_user_vk(session, vk_id)
    profile = await get_or_create_quiz_profile(session, user)

    step = get_current_step(profile)
    selected = profile.data.get("_selected")

    if not validate_next(selected):
        # Временное предупреждение — удалится через 2 секунды
        try:
            result = await vk_api.messages.send(
                peer_id=peer_id,
                message="⚠️ Выберите вариант, затем нажмите «Далее»",
                random_id=random.randint(1, 2 ** 31),
            )
            if result:
                await asyncio.sleep(2)
                with contextlib.suppress(Exception):
                    await vk_api.messages.delete(
                        message_ids=[result],
                        delete_for_all=True,
                    )
        except Exception:
            pass
        return

    await save_and_next(session=session, profile=profile, step=step, selected_option=selected)
    profile.data.pop("_selected", None)
    session.add(profile)
    await session.commit()

    if profile.completed:
        # Убираем квиз-сообщение
        if cmid:
            with contextlib.suppress(Exception):
                await vk_api.messages.delete(
                    peer_id=peer_id,
                    conversation_message_ids=[cmid],
                    delete_for_all=True,
                )

        if profile.completed_once:
            # Повторное прохождение
            await _send(vk_api, peer_id,
                        "✅ Квиз завершён"
                        "\n\nВаши ответы обновлены и учтены новые данные",
                        keyboard=vk_kb.ai_mode_kb())
            return

        # Первое прохождение — GIF + текст + кнопка
        profile.completed_once = True
        session.add(profile)
        await session.commit()

        await _send(
            vk_api, peer_id,
            "✅ Отлично! Квиз-опрос завершён\n\n"
            "Теперь у меня есть некоторое понимание ситуации. Данные из Ваших ответов помогут мне выдавать советы и "
            "подбирать модели именно под ваши условия — будь то поиск новой коляски или малоизвестные нюансы ухода "
            "за той, что уже стоит у Вас дома\n\n"
            "Если захотите что-то изменить в ответах, это всегда можно сделать тут:\n"
            "[📋 Меню] >> [👤 Мой профиль]\n\n"
            "Остался последний шаг - открыть доступ к подбору, советам и рекомендациям",
            attachment=VK_UPLOADED_PHOTOS.get("gif_finish"),
            keyboard=vk_kb.kb_activation(),
        )
        return

    # Переход на следующий шаг — редактируем текущее сообщение
    if cmid:
        await _render_quiz_step_vk(vk_api, peer_id, profile, cmid=cmid)
    else:
        await _render_quiz_step_vk(vk_api, peer_id, profile, send_new=True)


async def _handle_quiz_back(vk_id, peer_id, session, vk_api, cmid=None):
    """Кнопка «Назад»."""
    user = await get_or_create_user_vk(session, vk_id)
    profile = await get_or_create_quiz_profile(session, user)
    await go_back(session, profile)

    if cmid:
        await _render_quiz_step_vk(vk_api, peer_id, profile, cmid=cmid)
    else:
        await _render_quiz_step_vk(vk_api, peer_id, profile, send_new=True)


async def _handle_quiz_restart(vk_id, peer_id, session, vk_api):
    """Рестарт квиза из профиля — сохраняем completed_once."""
    user = await get_or_create_user_vk(session, vk_id)
    profile = await get_or_create_quiz_profile(session, user)

    profile.branch = None
    profile.current_level = 1
    profile.completed = False
    profile.data = {}
    session.add(profile)
    await session.commit()

    await _render_quiz_step_vk(vk_api, peer_id, profile, send_new=True)


async def _render_quiz_step_vk(vk_api, peer_id, profile, selected=None,
                                cmid=None, session=None, send_new=False):
    """Рендерит шаг квиза для VK.

    cmid — conversation_message_id для редактирования.
    send_new=True — отправить новым сообщением (для смены фото).
    """
    try:
        branch = profile.branch or "root"
        step = QUIZ_CONFIG[branch][profile.current_level]
    except KeyError:
        await _send(vk_api, peer_id, "❌ Ошибка квиза. Попробуйте заново.",
                    keyboard=vk_kb.quiz_start_kb())
        return

    text = _get_quiz_text_vk(step, selected)
    keyboard = vk_kb.build_quiz_keyboard(step, profile, selected)
    photo_vk = _get_quiz_photo_vk(step, selected)

    if cmid and not send_new:
        # РЕДАКТИРУЕМ существующее сообщение (выбор варианта)
        await _edit(vk_api, peer_id, cmid, text, keyboard=keyboard, attachment=photo_vk)
    else:
        # ОТПРАВЛЯЕМ новое сообщение (новый шаг, новое фото)
        await _send(vk_api, peer_id, text, keyboard=keyboard, attachment=photo_vk)


# ============================================================
# SERVICE / EMAIL / MASTER
# ============================================================

async def _handle_stroller_model(text, vk_id, peer_id, session, vk_api):
    """Запись модели коляски на ТО."""
    try:
        await session.execute(
            update(User).where(User.vk_id == vk_id).values(
                stroller_model=text,
                service_registered_at=datetime.now(timezone.utc),
                service_level=0,
            )
        )
        await session.commit()
    except Exception as e:
        logger.error(f"Service register error: {e}")
        await _send(vk_api, peer_id, "Ошибка при записи. Попробуйте позже.")
        return

    await _send(vk_api, peer_id,
                "✅ Ваша коляска поставлена на учет!\n\n"
                    f"Модель: {text}\n\n"
                    "Уведомление придет, когда настанет время для ТО. "
                    "Система учитывает особенности вашей модели и текущее время года, "
                    "чтобы напомнить о профилактике ровно тогда, когда это действительно необходимо 🗓\n\n"
                    "Мониторинг запущен ⚙️\n"
                    "Главное — не удаляйте этот чат и не перезагружайте бота, иначе данные о пробеге и индивидуальные "
                    "настройки вашей коляски обнулятся"
                )


async def _handle_email_input(text, vk_id, peer_id, session, vk_api):
    """Сохранение email."""
    import re
    email = text.strip().lower()

    if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
        await _send(vk_api, peer_id,
                    "❌ Некорректный формат email. Попробуйте ещё раз:")
        await _set_state(vk_id, "state", "waiting_email", ex=300)
        return

    await session.execute(
        update(User).where(User.vk_id == vk_id).values(email=email)
    )
    await session.commit()
    await _send(vk_api, peer_id, f"✅ Email сохранен: {email}")


async def _handle_master_start(vk_id, peer_id, vk_api):
    """Аналог /master26."""
    await _send(
        vk_api, peer_id,
        "📬 Код принят. Прямая линия открыта\n\n"
        "Сюда можно присылать вопросы по ремонту, муки выбора, "
        "истории удачных покупок или жалобы на магазины.\n\n"
        "Нажмите «Поделиться историей» чтобы начать:",
        keyboard=vk_kb.master_start_kb(),
    )


async def _handle_master_text(text, vk_id, peer_id, vk_api):
    """Приём текста обращения к мастеру."""
    # Пересылаем в канал через Telegram-бот (если доступен)
    # В VK-версии просто логируем
    logger.info(f"VK Master feedback from {vk_id}: {text[:200]}")
    await _send(vk_api, peer_id,
                "✅ Послание отправлено!\n\n"
                "Если это интересный случай — обсудим в канале! Спасибо 👍")


# ============================================================
# УТИЛИТЫ
# ============================================================

async def _edit(vk_api: API, peer_id: int, conversation_message_id: int,
                text: str, keyboard: str = None, attachment: str = None):
    """Редактирует сообщение бота в VK (аналог edit_message в Telegram)."""
    try:
        kwargs = {
            "peer_id": peer_id,
            "conversation_message_id": conversation_message_id,
            "message": text or " ",
        }
        if keyboard:
            kwargs["keyboard"] = keyboard
        if attachment:
            kwargs["attachment"] = attachment

        await vk_api.messages.edit(**kwargs)
    except Exception as e:
        logger.error(f"VK edit error (cmid={conversation_message_id}): {e}")


async def _send(vk_api: API, peer_id: int, text: str, keyboard: str = None, attachment: str = None):
    """Отправка сообщения через VK API."""
    try:
        # VK требует непустое сообщение
        if not text and not attachment:
            text = " "
        # VK имеет лимит 4096 символов на сообщение
        if len(text or "") > 4000:
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for i, chunk in enumerate(chunks):
                await vk_api.messages.send(
                    peer_id=peer_id, message=chunk,
                    random_id=random.randint(1, 2**31),
                    keyboard=keyboard if i == len(chunks) - 1 else None,
                    attachment=attachment if i == 0 else None,
                    dont_parse_links=1,
                )
        else:
            await vk_api.messages.send(
                peer_id=peer_id, message=text or " ",
                random_id=random.randint(1, 2**31),
                keyboard=keyboard, attachment=attachment,
                dont_parse_links=1,
            )
    except Exception as e:
        logger.error(f"VK send error to {peer_id}: {e}")


def _parse_payload(message: dict) -> dict | None:
    """Парсит payload из сообщения VK."""
    raw = message.get("payload")
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None


def _strip_html(text: str) -> str:
    """Убирает HTML-теги из текста для VK."""
    import re
    # Заменяем <b>text</b> на text
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<blockquote>(.*?)</blockquote>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<a\s+href=[\'"]([^\'"]*)[\'"][^>]*>(.*?)</a>', r'\2\n\1', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    return text
