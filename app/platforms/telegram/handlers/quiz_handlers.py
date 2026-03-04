import os
from aiogram.types import FSInputFile
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import User
from app.core.db.crud import get_or_create_user
from app.core.quiz.photo_ids import TG_UPLOADED_PHOTOS
from app.platforms.telegram.keyboards import kb_activation
from app.core.quiz.config_quiz import QUIZ_CONFIG
from app.core.quiz.renderer import render_quiz_step, resolve_media, build_keyboard
from app.core.quiz.quiz_state_service import (
    get_or_create_quiz_profile,
    get_current_step,
    validate_next,
    save_and_next,
    go_back,
)

quiz_router = Router()



#Старт квиза (после нажатия кнопки запуска квиза)
# 🔹 Что происходит:
# получаем пользователя
# cоздаём / восстанавливаем профиль квиза
# рендерим текущий шаг (или первый)
@quiz_router.callback_query(F.data == "quiz:start")
async def quiz_start(
    call: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
):
    # 1. Сразу отвечаем на колбэк
    await call.answer()

    # 2. Получаем пользователя
    user = await get_or_create_user(
        session=session,
        telegram_id=call.from_user.id,
        username=call.from_user.username,
    )

    # 3. Получаем профиль
    profile = await get_or_create_quiz_profile(session, user)

    # 🔥 ИСПРАВЛЕНИЕ: ПРИНУДИТЕЛЬНЫЙ СБРОС ПРОГРЕССА
    # Так как мы запускаем квиз с "Главного меню", мы должны обнулить всё старое
    profile.branch = None
    profile.current_level = 1
    profile.completed = False
    profile.completed_once = False
    profile.data = {}  # Очищаем все сохраненные ответы

    # Сохраняем "чистый" профиль в БД
    session.add(profile)
    await session.commit()

    try:
        await bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )
    except Exception as e:
        print(f"Не удалось убрать кнопки с видео: {e}")

    # Первый шаг — отправляем фото-квиз заново
    step = get_current_step(profile)
    photo, text = resolve_media(step, None)
    msg = await bot.send_photo(
        chat_id=call.message.chat.id,
        photo=photo,
        caption=text,
        reply_markup=build_keyboard(step, profile, None)
    )

    # сохраняем message_id нового фото-сообщения для редактирования
    profile.quiz_message_id = msg.message_id
    session.add(profile)
    await session.commit()





# Выбор варианта (кнопка 1 / 2 / 3 и т.д.)
# callback вида: quiz:select:<option_key>
# 🔹 Зачем:
# пользователь может тыкать разные кнопки
# «Далее» станет активным
# ничего в БД «навсегда» не пишем
@quiz_router.callback_query(F.data.startswith("quiz:select:"))
async def quiz_select_option(
    call: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
):
    selected_option = call.data.split(":")[2]

    user = await get_or_create_user(
        session=session,
        telegram_id=call.from_user.id,
        username=call.from_user.username,
    )
    profile = await get_or_create_quiz_profile(session, user)

    # 🔹 ТОЛЬКО временный выбор
    profile.data["_selected"] = selected_option
    session.add(profile)
    await session.commit()

    await render_quiz_step(
        bot=bot,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        profile=profile,
        selected=selected_option,
    )
    await call.answer()





# Кнопка «Далее»
# 🔹 ВАЖНО:
# только тут данные навсегда пишутся в БД
# ветка определяется
# уровень увеличивается
@quiz_router.callback_query(F.data == "quiz:next")
async def quiz_next(
    call: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
):
    user = await get_or_create_user(
    session=session,
    telegram_id=call.from_user.id,
    username=call.from_user.username,
)
    profile = await get_or_create_quiz_profile(session, user)

    step = get_current_step(profile)
    selected_option = profile.data.get("_selected")

    # ⛔ запрет «Далее» без выбора
    if not validate_next(selected_option):
        await call.answer(
            "Выберите вариант и затем нажмите кнопку «Далее»",
            show_alert=True,
        )
        return

    await save_and_next(
        session=session,
        profile=profile,
        step=step,
        selected_option=selected_option,
    )

    # очищаем временный выбор
    profile.data.pop("_selected", None)
    session.add(profile)
    await session.commit()

    # 🏁 КВИЗ ЗАВЕРШЁН
    if profile.completed:
        # 1️⃣ удаляем сообщение квиза
        try:
            await call.message.delete()
        except:
            pass

            # 🔁 ПОВТОРНОЕ ПРОХОЖДЕНИЕ
        if profile.completed_once:
            await call.message.answer(
                "✅ Квиз завершён"
                "\n\nВаши ответы обновлены и учтены новые данные"
                "\n\n/ai_consultant — <b>Начать умный подбор</b>"
            )
            return

        # 2️⃣ отправляем GIF + текст + кнопку (первое прохождение)
        profile.completed_once = True
        session.add(profile)
        await session.commit()
        # Пытаемся отправить гифку
        try:
            # Сначала пробуем отправить по ID (если он валидный)
            await bot.send_animation(
                chat_id=call.message.chat.id,
                animation=TG_UPLOADED_PHOTOS.get("gif_finish"),
                caption=(
                    "✅ <b>Отлично! Квиз-опрос завершён</b>\n\n"
                    "Теперь у меня есть некоторое понимание ситуации. Данные из Ваших ответов помогут мне выдавать советы и "
                    "подбирать модели именно под ваши условия — будь то поиск новой коляски или малоизвестные нюансы ухода "
                    "за той, что уже стоит у Вас дома"
                    "<blockquote>Если захотите что-то изменить в ответах, это всегда можно сделать тут:\n"
                    "<b>[Меню] >> [👤 Мой профиль]</b></blockquote>\n\n"
                    "<b>Остался последний шаг - открыть доступ к подбору, советам и рекомендациям</b>"
                ),
                reply_markup=kb_activation,
            )
        except Exception as e:
            #ЕСЛИ ID СТАРЫЙ ИЛИ НЕ РАБОТАЕТ — ЖЕЛЕЗОБЕТОННЫЙ ВАРИАНТ С ДИСКА
            # Высчитываем путь до корня проекта (PROkolyaski)
            CURRENT_FILE = os.path.abspath(__file__)
            ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(CURRENT_FILE))))

            GIF_PATH = os.path.join(ROOT_DIR, "mediafile_for_bot", "gif_finish.gif")

            gif_file = FSInputFile(GIF_PATH)

            await bot.send_animation(
                chat_id=call.message.chat.id,
                animation=gif_file,
                caption=(
                    "✅ <b>Отлично! Квиз-опрос завершён</b>\n\n"
                    "Теперь у меня есть некоторое понимание ситуации. Данные из Ваших ответов помогут мне выдавать советы и "
                    "подбирать модели именно под ваши условия — будь то поиск новой коляски или малоизвестные нюансы ухода "
                    "за той, что уже стоит у Вас дома"
                    "<blockquote>Если захотите что-то изменить в ответах, это всегда можно сделать тут:\n"
                    "<b>[Меню] >> [👤 Мой профиль]</b></blockquote>\n\n"
                    "<b>Остался последний шаг - открыть доступ к подбору, советам и рекомендациям</b>"
                ),
                reply_markup=kb_activation,
            )
        return

    # иначе — обычный переход
    await render_quiz_step(
        bot=bot,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        profile=profile,
        selected=None,
    )





# Кнопка «Назад»
# 🔹 Что делает:
# откатывает уровень
# удаляет сохранённое значение шага
# корректно работает в ветках
@quiz_router.callback_query(F.data == "quiz:back")
async def quiz_back(
    call: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
):
    await call.answer()
    user = await get_or_create_user(
        session=session,
        telegram_id=call.from_user.id,
        username=call.from_user.username,
    )
    profile = await get_or_create_quiz_profile(session, user)

    await go_back(session, profile)

    await render_quiz_step(
        bot=bot,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        profile=profile,
        selected=None,
    )


# Сброс и перезапуск квиз-формы
@quiz_router.message(Command("quiz_restart"))
async def restart_quiz_cmd(
    message: Message,
    bot: Bot,
    session: AsyncSession,
):
    user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
    profile = await get_or_create_quiz_profile(session, user)

    profile.branch = None
    profile.current_level = 1
    profile.completed = False
    profile.data = {}

    session.add(profile)
    await session.commit()

    step = get_current_step(profile)
    photo, text = resolve_media(step, None)

    await message.answer_photo(
        photo=photo,
        caption=text,
        reply_markup=build_keyboard(step, profile, None)
    )


# Этот код срабатывает ТОЛЬКО при нажатии кнопки восстановления после ошибки
@quiz_router.callback_query(F.data == "quiz:restore")
async def quiz_restore_session(call: CallbackQuery, bot: Bot, session: AsyncSession):
    await call.answer()

    # 1. Получаем профиль (как есть, без изменений!)
    user = await get_or_create_user(session, call.from_user.id, call.from_user.username)
    profile = await get_or_create_quiz_profile(session, user)

    # 2. Удаляем сообщение с ошибкой (на котором была кнопка)
    try:
        await call.message.delete()
    except:
        pass

    # 3. Просто рендерим ТЕКУЩИЙ шаг заново
    # Берем данные из профиля, который мы НЕ обнуляли
    branch = profile.branch or "root"
    current_level = profile.current_level

    # Защита от несуществующего уровня
    try:
        step = QUIZ_CONFIG[branch][current_level]
    except KeyError:
        # Если вдруг уровень сломан — вот тогда сбрасываем
        profile.current_level = 1
        await session.commit()
        step = QUIZ_CONFIG["root"][1]

    photo, text = resolve_media(step, None)

    # Отправляем НОВОЕ сообщение (так как старое редактировать не вышло)
    msg = await bot.send_photo(
        chat_id=call.message.chat.id,
        photo=photo,
        caption=text,
        reply_markup=build_keyboard(step, profile, None)
    )

    # Обновляем ID сообщения в базе
    profile.quiz_message_id = msg.message_id
    session.add(profile)
    await session.commit()
