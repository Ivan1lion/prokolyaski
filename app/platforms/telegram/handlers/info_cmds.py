import os
import asyncio
import logging
from aiogram import F, Router, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.sql import func

from app.core.db.models import User
from app.core.db.crud import closed_menu
import app.platforms.telegram.keyboards as kb
from app.core.redis_client import redis_client
from app.platforms.telegram.handlers.states import ServiceState


logger = logging.getLogger(__name__)
info_router = Router()


tech_channel_id = int(os.getenv("TECH_CHANNEL_ID"))
guide_post = int(os.getenv("GUIDE_POST"))
rules_post = int(os.getenv("RULES_POST"))
manual_post = int(os.getenv("MANUAL_POST"))
manual2_post = int(os.getenv("MANUAL2_POST"))


@info_router.message(Command("guide"))
async def guide_cmd(message: Message, bot:Bot, session: AsyncSession):
    if await closed_menu(message=message, session=session):
        return
    # # 1. Пытаемся отправить мгновенно через Redis (PRO способ)
    # # Мы ищем file_id, который сохранили под именем "guide_video"
    text = (f"📝 <b>Шпаргалка: «Что нужно учитывать при подборе»</b>"
            f"\n\n<b>1. Основа:</b>"
            f"\n\n• Тип коляски (от рождения или прогулка)"
            f"\n• Функционал (2в1, 3в1 или просто люлька)"
            f"\n• Формат использования (для прогулок или путешествий)"
            f"\n• Сезон (зима или лето)"
            f"\n• Тип дорог (грунт, асфальт или бездорожье)"
            f"\n\n👆 <i>Эти вопросы мы закрыли в самом начале, когда Вы проходили квиз-опрос. Это база (фундамент) "
            f"для поиска и подбора подходящей коляски</i>"
            f"\n\n<b>2. Жизненные нюансы (на этом часто «спотыкаются»):</b>"
            f"\n<i>Негативное влияние этих деталей вы также можете почувствовать на себе уже после покупки, если "
            f"не учтете их сейчас</i>"
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
            f"<blockquote>«Живу на 5 этаже без лифта, узкие двери, муж высокий, бюджет 40к»</blockquote>"
            f"\n\n/ai_consultant — <b>Начать умный подбор</b>"
            f"\n\n/quiz_restart — <b>Перепройти квиз (базу)</b>"
            )

    # === ПОПЫТКА 1: REDIS (Теперь безопасная) ===
    video_id = await redis_client.get("media:guide_video")
    if video_id:
        try:
            await message.answer_video(
                video=video_id,
                caption=f"<b>Если видео долго грузится можете просмотреть его тут:</b>"
                        f"\n\nYouTube - https://www.youtube.com/"
                        f"\n\nRUTUBE - https://rutube.ru/"
                        f"\n\nVK Видео - https://vkvideo.ru/"
            )
            await message.answer(text=text)
            print(f"🔔 ПОПЫТКА 1: Redis)")
            return  # Успех, выходим
        except Exception as e:
            logger.error(f"Ошибка отправки video_note из Redis: {e}")

    # 2. FALLBACK 1: Если в Redis пусто, пробуем copy_message (Старый способ)
    # Это страховка на случай, если ты забыл загрузить видео в тех.канал
    try:
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=tech_channel_id,  # ID тех канала
            message_id=guide_post,  # ID сообщения из группы
            caption=f"<b>Если видео долго грузится можете просмотреть его тут:</b>"
                    f"\n\nYouTube - https://www.youtube.com/"
                    f"\n\nRUTUBE - https://rutube.ru/"
                    f"\n\nVK Видео - https://vkvideo.ru/"
        )
        await message.answer(text=text)
        print(f"🔔 ПОПЫТКА 2: Пересылка из канала)")
        return
    except Exception as e:
        logger.error(f"❌ FALLBACK 1 failed: {e}")

    logger.error("❌ Redis и технический канал недоступны")
    await message.answer(
        text=f"<b>Выберите, где Вам удобнее просмотреть видео:</b>"
             f"\n\nYouTube - https://www.youtube.com/"
             f"\n\nRUTUBE - https://rutube.ru/"
             f"\n\nVK Видео - https://vkvideo.ru/"
             f"\n\n{text}"
    )




# Выносим всю логику отправки в отдельную функцию (DRY - Don't Repeat Yourself)
async def send_rules_video(message: Message, bot: Bot):
    # # 1. Пытаемся отправить мгновенно через Redis (PRO способ)
    # # Мы ищем file_id, который сохранили под именем "rules_video"
    # === ПОПЫТКА 1: REDIS (Теперь безопасная) ===
    video_id = await redis_client.get("media:rules_video")
    if video_id:
        try:
            await message.answer_video(
                video=video_id,
                caption=f"<b>Если видео долго грузится можете просмотреть его тут:</b>"
                        f"\n\nYouTube - https://www.youtube.com/"
                        f"\n\nRUTUBE - https://rutube.ru/"
                        f"\n\nVK Видео - https://vkvideo.ru/"
            )
            print(f"🔔 ПОПЫТКА 1: Redis)")
            return  # Успех, выходим
        except Exception as e:
            logger.error(f"Ошибка отправки video_note из Redis: {e}")

    # 2. FALLBACK 1: Если в Redis пусто, пробуем copy_message (Старый способ)
    # Это страховка на случай, если ты забыл загрузить видео в тех.канал
    try:
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=tech_channel_id,  # ID тех канала
            message_id=rules_post,  # ID сообщения из группы
            caption=f"<b>Если видео долго грузится можете просмотреть его тут:</b>"
                    f"\n\nYouTube - https://www.youtube.com/"
                    f"\n\nRUTUBE - https://rutube.ru/"
                    f"\n\nVK Видео - https://vkvideo.ru/"
        )
        print(f"🔔 ПОПЫТКА 2: Пересылка из канала)")
        return
    except Exception as e:
        logger.error(f"❌ FALLBACK 1 failed: {e}")

    logger.error("❌ Redis и технический канал недоступны")
    await message.answer(
        text=f"<b>Выберите, где Вам удобнее просмотреть видео:</b>"
             f"\n\nYouTube - https://www.youtube.com/"
             f"\n\nRUTUBE - https://rutube.ru/"
             f"\n\nVK Видео - https://vkvideo.ru/"
    )


# ХЭНДЛЕР 1: Реакция на команду /manual
@info_router.message(Command("rules"))
async def service_cmd(message: Message, bot: Bot, session: AsyncSession):
    if await closed_menu(message=message, session=session):
        return

    # Передаем работу нашей функции
    await send_rules_video(message, bot)


# ХЭНДЛЕР 2: Реакция на инлайн-кнопку "rules_mode"
@info_router.callback_query(F.data == "rules_mode")
async def service_callback(callback: CallbackQuery, bot: Bot, session: AsyncSession):
    # Обязательно отвечаем на коллбэк, чтобы у юзера пропали "часики" загрузки на кнопке
    await callback.answer()

    # Вызываем ту же самую функцию отправки!
    # Передаем callback.message, чтобы видео улетело в правильный чат
    await send_rules_video(callback.message, bot)





@info_router.message(Command("manual"))
async def service_cmd(message: Message, bot:Bot, session: AsyncSession):
    if await closed_menu(message=message, session=session):
        return
    # # 1. Пытаемся отправить мгновенно через Redis (PRO способ)
    # # Мы ищем file_id, который сохранили под именем "manual_video"
    # === ПОПЫТКА 1: REDIS (Теперь безопасная) ===
    video_id = await redis_client.get("media:manual_video")
    if video_id:
        try:
            await message.answer_video(
                video=video_id,
                caption=f"<b>Если видео долго грузится можете просмотреть его тут:</b>"
                        f"\n\nYouTube - https://www.youtube.com/"
                        f"\n\nRUTUBE - https://rutube.ru/"
                        f"\n\nVK Видео - https://vkvideo.ru/",
                reply_markup=kb.next_service
            )
            print(f"🔔 ПОПЫТКА 1: Redis)")
            return  # Успех, выходим
        except Exception as e:
            logger.error(f"Ошибка отправки video_note из Redis: {e}")

    # 2. FALLBACK 1: Если в Redis пусто, пробуем copy_message (Старый способ)
    # Это страховка на случай, если ты забыл загрузить видео в тех.канал
    try:
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=tech_channel_id,  # ID тех канала
            message_id=manual_post,  # ID сообщения из группы
            caption=f"<b>Если видео долго грузится можете просмотреть его тут:</b>"
                    f"\n\nYouTube - https://www.youtube.com/"
                    f"\n\nRUTUBE - https://rutube.ru/"
                    f"\n\nVK Видео - https://vkvideo.ru/",
            reply_markup=kb.next_service
        )
        print(f"🔔 ПОПЫТКА 2: Пересылка из канала)")
        return
    except Exception as e:
        logger.error(f"❌ FALLBACK 1 failed: {e}")

    logger.error("❌ Redis и технический канал недоступны")
    await message.answer(
        text=f"<b>Выберите, где Вам удобнее просмотреть видео:</b>"
             f"\n\nYouTube - https://www.youtube.com/"
             f"\n\nRUTUBE - https://rutube.ru/"
             f"\n\nVK Видео - https://vkvideo.ru/",
        reply_markup=kb.next_service
    )




@info_router.callback_query(F.data == "next_service")
async def process_next_manual_button(callback: CallbackQuery):
    # 1. Удаляем сообщение с видео и кнопкой
    try:
        await callback.message.delete()
    except Exception as e:
        logger.error(f"Не удалось удалить сообщение: {e}")

    # 2. Формируем текст
    text = (
        "📌 <b>Памятка: 3 способа как не убить коляску</b>"
        "\n\n🚿 <b>Никакого душа</b>"
        "<blockquote>Не мойте колеса из шланга или в ванной. Вода вымоет смазку и подшипники сгниют "
        "за месяц. Только влажная тряпка</blockquote>"
        "\n\n🏋️ <b>Осторожнее с ручкой</b>"
        "<blockquote>Не давите на неё всем весом перед бордюром — всегда помогайте ногой, "
        "наступая на заднюю ось. Иначе разболтаете механизм складывания (а это самый дорогой ремонт)</blockquote>"
        "\n\n🛢 <b>Забудьте про WD-40</b>"
        "<blockquote>Вэдэшка 'сушит' подшипники, а любые бытовые масла работают как магнит для песка — через неделю "
        "механизмы захрустят еще сильнее. Металл и пластик колясок смазывают только силиконом</blockquote>"
        "\n\nСмазку, которой я пользуюсь в мастерской, обычно покупаю у своего поставщика запчастей и прочих "
        "расходников. На валдберриз такую же не нашел, но нашел с такими же характеристиками, соотношение газа к "
        "масляному раствору отличное и по цене норм"
        # "\n\n<a href='https://www.wildberries.ru/catalog/191623733/detail.aspx?targetUrl=MI'>Смазка силиконовая "
        # "для колясок https://www.wildberries.ru/catalog/191623733/detail.aspx?targetUrl=MI</a>"
        "\n\nЕсли смазывать только коляску, то флакона хватит на пару лет"
        "\n<blockquote><i>👆 Если что памятка хранится в разделе</i> "
        "\n[👤 Мой профиль]</blockquote>"
        "\n\n/service - Встать на плановое ТО"
    )

    # 3. Отправка видео-кружка
    video_sent = False

    # Попытка 1: Берем file_id из Redis (замените "media:service_video" на ваш ключ)
    video_note_id = await redis_client.get("media:manual2_video")

    if video_note_id:
        try:
            await callback.message.answer_video_note(video_note=video_note_id)
            video_sent = True
        except Exception as e:
            logger.error(f"Ошибка отправки video_note из Redis: {e}")

    # Попытка 2: Fallback — копируем из тех. канала, если Redis пуст или выдал ошибку
    if not video_sent:
        try:
            await bot.copy_message(
                chat_id=callback.message.chat.id,
                from_chat_id=tech_channel_id,
                message_id=manual2_post,
            )
        except Exception as e:
            logger.error(f"Ошибка копирования кружка из канала: {e}")

    # 4. Пауза для сохранения визуального порядка (кружок -> текст)
    await asyncio.sleep(1)

    # 5. Отправляем новое сообщение (disable_web_page_preview убирает огромное превью от ссылки на WB)
    await callback.message.answer(
        text=text,
        reply_markup=kb.get_wb_link,
        disable_web_page_preview=True
    )

    # 6. "Гасим" часики на кнопке
    await callback.answer()





@info_router.callback_query(F.data == "get_wb_link")
async def process_get_wb_link(callback: CallbackQuery, session: AsyncSession):
    user_id = callback.from_user.id

    # 1. ЗАПИСЬ В АНАЛИТИКУ (Если первый клик)
    stmt = select(User.wb_clicked_at).where(User.telegram_id == user_id)
    clicked_at = (await session.execute(stmt)).scalar_one_or_none()

    if clicked_at is None:
        await session.execute(
            update(User).where(User.telegram_id == user_id).values(wb_clicked_at=func.now())
        )
        await session.commit()
    # 2. ФОРМИРУЕМ НОВЫЙ ТЕКСТ (С добавленной ссылкой)
    new_text = (
        "<a href='https://www.wildberries.ru/catalog/274474180/detail.aspx'>Смазка силиконовая "
        "для колясок https://www.wildberries.ru/catalog/274474180/detail.aspx</a>"
    )
    # 3. РЕДАКТИРУЕМ СООБЩЕНИЕ
    try:
        await callback.message.edit_text(
            text=new_text,
            # 🔥 ВАЖНО: Включаем превью, чтобы Telegram сам подтянул картинку товара!
            disable_web_page_preview=False,
            # 🔥 ВАЖНО: Убираем клавиатуру (кнопку), передав None
            reply_markup=None
        )
    except Exception as e:
        # Игнорируем ошибку, если юзер зачем-то дважды быстро кликнул и текст не изменился
        pass
    # 4. Убираем "часики" на кнопке
    await callback.answer()




@info_router.message(Command("service"))
async def cmd_service(message: Message, state: FSMContext):
    text = (
        "🛠 <b>Запись на плановое ТО</b>\n\n"
        "Пожалуйста, напишите марку и модель вашей коляски одним сообщением "
        "\n\n(например: <i>Tutis Uno 3+</i>, <i>Cybex Priam</i> или <i>Anex m/type</i>)."
    )
    await message.answer(text=text)
    # Включаем состояние "ожидание ввода модели"
    await state.set_state(ServiceState.waiting_for_model)



@info_router.message(StateFilter(ServiceState.waiting_for_model), F.text)
async def process_stroller_model(message: Message, state: FSMContext, session: AsyncSession):
    user_model = message.text
    user_id = message.from_user.id

    # 1. Записываем модель, время старта и обнуляем уровень рассылки
    try:
        stmt = (
            update(User)
            .where(User.telegram_id == user_id)
            .values(
                stroller_model=user_model,
                service_registered_at=datetime.now(timezone.utc), # Фиксируем точное время
                service_level=0 # Сбрасываем счетчик сообщений
            )
        )
        await session.execute(stmt)
        await session.commit()
    except Exception as e:
        logger.error(f"Ошибка при записи модели коляски для ТО: {e}")
        await message.answer("Произошла ошибка при записи. Пожалуйста, попробуйте позже")
        await state.clear()
        return

    # 2. Выключаем состояние (выходим из FSM)
    await state.clear()

    # 3. Отправляем подтверждение юзеру
    success_text = (
        "✅ <b>Ваша коляска поставлена на учет!</b>\n\n"
        "<b>Модель:</b> <i>{user_model}</i>\n\n"
        "Уведомление придет, когда настанет время для ТО. "
        "Система учитывает особенности вашей модели и текущее время года, "
        "чтобы напомнить о профилактике ровно тогда, когда это действительно необходимо 🗓\n\n"
        "<b>Мониторинг запущен</b> ⚙️\n"
        "<blockquote>Главное — не удаляйте этот чат и не перезагружайте бота, иначе данные о пробеге и индивидуальные "
        "настройки вашей коляски обнулятся</blockquote>"
    )

    await message.answer(text=success_text)



#Реакция юзера на тестовое первое уведомление о плановом ТО
@info_router.callback_query(F.data.in_(["to_feed_like", "to_feed_dislike"]))
async def process_to_feedback(callback: CallbackQuery, session: AsyncSession):
    user_id = callback.from_user.id

    # 1. Определяем, что нажал юзер
    feedback_value = "like" if callback.data == "to_feed_like" else "dislike"

    # 2. Записываем результат в базу данных
    stmt = (
        update(User)
        .where(User.telegram_id == user_id)
        .values(first_to_feedback=feedback_value)
    )
    await session.execute(stmt)
    await session.commit()

    # 3. Убираем клавиатуру (кнопки исчезнут)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass  # Игнорируем ошибку, если вдруг сообщение уже старое

    # 4. Отправляем всплывающее уведомление (alert)
    if feedback_value == "like":
        await callback.answer("Спасибо! Рад, что могу быть полезным 🤝", show_alert=True)
    else:
        await callback.answer("Спасибо за честный отзыв. Буду улучшать контент! 🤝", show_alert=True)