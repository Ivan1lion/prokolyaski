import os
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from aiogram.fsm.context import FSMContext


from app.platforms.telegram.handlers.user_handlers import AIChat
from app.core.db.models import Payment
from app.core.db.crud import closed_menu
from app.core.quiz.photo_ids import TG_UPLOADED_PHOTOS
from app.core.redis_client import redis_client

help_router = Router()
logger = logging.getLogger(__name__)


tech_channel_id = int(os.getenv("TECH_CHANNEL_ID"))
my_username = os.getenv("MASTER_USERNAME")




# ---  КОНФИГУРАЦИЯ ---
# Ключ: Команда
# Значение: Словарь с ключом для Redis и ID сообщения в канале
FAQ_CONFIG = {
    "/faq_1": {
        "redis_key": "media:faq_1",
        "msg_id": 39  # 👈 Замените на реальный ID сообщения в канале
    },
    "/faq_2": {
        "redis_key": "media:faq_2",
        "msg_id": 40
    },
    "/faq_3": {
        "redis_key": "media:faq_3",
        "msg_id": 41
    },
    "/faq_4": {
        "redis_key": "media:faq_4",
        "msg_id": 42
    },
}


# --- 1. Основное меню /help ---
@help_router.message(Command("help"))
async def help_cmd(message: Message, session: AsyncSession):
    if await closed_menu(message=message, session=session):
        return

    # Текст сообщения
    text = (
        f"<b>🆘 Проблемы и решения</b>\n\n"
        f"<b>1. Ответы на самые частые вопросы</b> (нажмите команду)<b>:</b>"
        f"\n\n/faq_1 - «Новая коляска скрипит! Мне продали брак?»"
        f"\n\n/faq_2 - «Как снять колеса»"
        f"\n\n/faq_3 - «Почему в люльке голова ниже ног?» (Или наоборот)"
        f"\n\n/faq_4 - «До скольки атмосфер качать колеса?»"
        f"\n\n〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"<b>2. Умный помощник</b>\n"
        f"Если у вас другой вопрос, попробуйте решить его с AI-консультантом. У него обширная база знаний"
        f"\n\n/ai_info - обратиться к AI"
        f"\n\n〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"<b>3. Связь с мастером</b>\n"
        f"Если бот не справился и случай реально тяжелый — пишите мне напрямую. Посмотрим что можно сделать"
    )

    # Кнопка связи с мастером
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать мастеру", callback_data="contact_master")]
    ])

    await message.answer(text, reply_markup=kb)



# --- 2. Умный обработчик (Lazy Loading) ---
@help_router.message(F.text.in_(FAQ_CONFIG.keys()))
async def send_faq_video(message: Message):
    """
    Ленивая загрузка видео через Redis кэш + технический канал
    """
    # 2. Получаем команду прямо из текста
    command = message.text
    config = FAQ_CONFIG.get(command)
    if not config:
        return

    redis_key = config["redis_key"]
    channel_msg_id = config["msg_id"]

    try:
        # 1. Сначала ищем быстрый file_id в Redis
        cached_file_id = await redis_client.get(redis_key)

        if cached_file_id:
            # 🚀 ВАРИАНТ А: Видео есть в кэше
            await message.answer_video(
                video=cached_file_id,
                caption=f"📹 Видео-ответ по запросу: {command}"
            )
            return

        # 🐢 ВАРИАНТ Б: В кэше пусто -> Берем из канала
        logger.info(f"🔄 Кэш пуст для {command}. Копирую из канала...")

        sent_msg = await message.bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=tech_channel_id,
            message_id=channel_msg_id,
            caption=f"📹 Видео-ответ по запросу: {command}"
        )


    except Exception as e:
        logger.error(f"❌ Ошибка Lazy Loading: {e}")
        await message.answer("Извините, видео временно недоступно. Перезагружаем сервер. Повторите попытку через 5 мин")



# --- 3. Переход в режим AI (по команде) ---
@help_router.message(Command("ai_info"))
async def start_ai_info_mode(message: Message, state: FSMContext, session: AsyncSession):
    if await closed_menu(message=message, session=session):
        return

    # 1. Устанавливаем состояние "Режим вопросов"
    await state.set_state(AIChat.info_mode)

    # 2. Отправляем сообщение (как в кнопке, но новым сообщением)
    await message.answer(
        "❓ <b>Режим: Вопрос эксперту</b>\n\n"
        "Я готов отвечать 🤖! Задайте любой вопрос по эксплуатации, ремонту или сравнению колясок"
        "\n\n<i>Например: «Что лучше: Anex или Tutis?» или «Как смазать колеса?»</i>"
    )


# --- 4. Логика проверки оплаты (contact_master) ---
@help_router.callback_query(F.data == "contact_master")
async def process_contact_master(callback: CallbackQuery, session: AsyncSession):
    # 1. Проверяем наличие успешной оплаты
    result = await session.execute(
        select(Payment).where(
            Payment.telegram_id == callback.from_user.id,
            Payment.status == 'succeeded'  # Только успешные
        ).limit(1)
    )
    has_payment = result.scalar_one_or_none()

    # СЦЕНАРИЙ А: Оплаты НЕТ
    if not has_payment:
        await callback.answer(
            "Лично отвечаю только на то, что не осилил 🤖AI-консультант"
            "\n\nСначала спросите AI, в 90% случаев этого хватает. Если он не решит вопрос — чат со мной откроется 💬",
            show_alert=True
        )
        return

    # СЦЕНАРИЙ Б: Оплата ЕСТЬ
    # Отправляем сообщение с кнопкой-ссылкой
    # (Мы не можем просто перекинуть юзера, нужно дать ему кнопку для перехода)
    await callback.message.answer_photo(
        photo=TG_UPLOADED_PHOTOS.get("for_help_master.jpg"),
        caption="✅ <b>Пришлите мне короткое видео (5-10 сек) и опишите или проговорите в самом видео "
                "суть Вашего вопроса</b>"
                "\n\nЯ стараюсь ответить и помочь всем кто пишет мне в ЛС, но не всегда могу сделать это оперативно. Как "
                "минимум у нас с Вами могут быть разные часовые пояса"
                "\n\nПрисылайте свой вопрос, как буду в мастерской на рабочем месте - сразу постараюсь ответить 😉",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📨 Перейти в диалог", url=f"https://t.me/{my_username}")]
        ])
    )
    await callback.answer()
