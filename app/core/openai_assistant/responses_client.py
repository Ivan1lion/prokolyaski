import os
import logging
import aiohttp
import re
import asyncio
from google import genai
from google.genai import types
from openai import AsyncOpenAI

# Настройка логирования
logger = logging.getLogger(__name__)

# === ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ ===

# 1. OpenAI (Резервный канал)
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 2. Google Gemini (Основной канал)
# Инициализируем клиент один раз.
# Асинхронные методы будут доступны через google_client.aio
google_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))


# ==========================================
# 🛠 ФУНКЦИИ ВАЛИДАЦИИ ССЫЛОК (POST-VALIDATION)
# ==========================================

async def check_url_status(session: aiohttp.ClientSession, url: str) -> bool:
    """
    Проверяет доступность ссылки.
    Пропускает проверку для поисковиков и маркетплейсов (защита от анти-бот систем).
    """
    # 1. СПИСОК ИСКЛЮЧЕНИЙ (Домены, которые блокируют ботов, но открываются у людей)
    TRUSTED_DOMAINS = [
        "yandex.ru", "yandex.com",
        "google.com", "google.ru",
        "ozon.ru", "wildberries.ru", "avito.ru",
        "youtube.com", "youtu.be"
    ]

    # Если ссылка ведет на доверенный домен — считаем её рабочей сразу
    if any(domain in url for domain in TRUSTED_DOMAINS):
        return True

    try:
        # 2. Маскируемся под браузер (на всякий случай для других сайтов)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        async with session.get(url, timeout=3, allow_redirects=True, headers=headers) as response:
            if response.status == 200:
                return True

            # Некоторые сайты возвращают 403 ботам, даже с заголовками.
            # Если это 403, можно рискнуть и вернуть True, но лучше логировать.
            if response.status == 403:
                logger.warning(f"⚠️ Доступ запрещен (403), но ссылка может быть рабочей: {url}")
                # Можно вернуть True, если доверяешь источнику
                return True

            logger.warning(f"❌ Битая ссылка (Status {response.status}): {url}")
            return False

    except asyncio.TimeoutError:
        logger.warning(f"⏳ Таймаут проверки ссылки: {url}")
        return True  # True, потому что не хочу браковать медленные сайты
    except Exception as e:
        logger.warning(f"❌ Ошибка проверки ссылки {url}: {e}")
        return False




async def validate_and_fix_links(text: str) -> str:
    """
    Находит HTML-ссылки <a href="...">Текст</a>, проверяет их.
    Если ссылка битая -> убирает тег <a>, оставляя только текст + пометку.
    """
    # 1. Регулярка для HTML ссылок
    # Группа 1: URL, Группа 2: Текст внутри тега
    link_pattern = re.compile(r'<a\s+href=[\'"](https?://[^\'"]+)[\'"][^>]*>(.*?)</a>', re.IGNORECASE)

    matches = link_pattern.findall(text)
    if not matches:
        return text

    # Собираем уникальные ссылки (URL - это первая группа)
    unique_urls = list(set(url for url, _ in matches))

    # Асинхронно проверяем
    async with aiohttp.ClientSession() as session:
        tasks = [check_url_status(session, url) for url in unique_urls]
        results = await asyncio.gather(*tasks)

    url_status = dict(zip(unique_urls, results))

    def replace_match(match):
        url = match.group(1)   # URL
        title = match.group(2) # Текст ссылки (например, название коляски)

        if url_status.get(url, False):
            # Ссылка живая - возвращаем как было
            return f'<a href="{url}">{title}</a>'
        else:
            # Ссылка мертвая - убираем тег, оставляем текст
            return f'{title}'

    fixed_text = link_pattern.sub(replace_match, text)
    return fixed_text




def clean_markdown_artifacts(text: str) -> str:
    """
    🔥 Очищает текст:
    1. Превращает Markdown (**жирный**) в HTML.
    2. Удаляет ВСЕ теги, кроме разрешенных Telegram-ом.
    """
    if not text:
        return ""

    # --- ЭТАП 1: Обработка Markdown ---

    # Жирный: **текст** -> <b>текст</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)

    # Заголовки: ## Текст -> <b>Текст</b>
    text = re.sub(r'#{2,}\s*(.*?)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # Списки: * или - в начале строки -> •
    text = re.sub(r'^\s*[\*\-]\s+', '• ', text, flags=re.MULTILINE)

    # --- ЭТАП 2: Умная очистка HTML (Белый список) ---

    # Список тегов, которые поддерживает Telegram
    ALLOWED_TAGS = {
        'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
        'a', 'code', 'pre', 'blockquote'
    }

    def clean_tag(match):
        full_tag = match.group(0)  # Весь тег целиком: <div class="x">
        tag_name = match.group(2).lower()  # Имя тега: div

        # 1. Исключения для читаемости:
        # <br> и </p> заменяем на перенос строки, иначе слова склеятся
        if tag_name == 'br':
            return '\n'
        if tag_name == 'p' and full_tag.startswith('</'):  # Закрывающий </p>
            return '\n'
        if tag_name == 'hr':  # Разделитель превращаем в линию
            return '〰️〰️〰️〰️〰️\n'

        # 2. Если тег в белом списке — оставляем как есть
        if tag_name in ALLOWED_TAGS:
            return full_tag

        # 3. Если тег неизвестен Telegram — УДАЛЯЕМ ЕГО (возвращаем пустоту)
        return ''

    # Регулярка ищет любые теги: </?tagName...>
    # Группа 1: Слэш (если есть)
    # Группа 2: Имя тега
    # Группа 3: Атрибуты и остальное
    text = re.sub(r'<(/?)(\w+)([^>]*)>', clean_tag, text)

    # Чистим двойные переносы, которые могли возникнуть
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text




# ==========================================
# 🧠 ОСНОВНАЯ ФУНКЦИЯ ЗАПРОСА
# ==========================================

async def ask_responses_api(user_message: str, system_instruction: str, use_google_search: bool = True, allow_fallback: bool = True) -> str:
    """
    Отправляет запрос к AI.
    Приоритет: Google Gemini 3 Pro -> Fallback: OpenAI.
    В конце выполняется проверка ссылок на валидность.
    """
    raw_answer = ""

    # ---------------------------------------------------------
    #     ПОПЫТКА 1: Google Gemini 3 Pro (Основной)
    # ---------------------------------------------------------
    try:
        # 🔥 Принт для понимания
        print(f"🔔 ПОПЫТКА 1: Google Gemini 3 Pro (Основной)")
        tools_config = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else []

        generate_config = types.GenerateContentConfig(
            temperature=1.0,
            system_instruction=system_instruction,
            tools=tools_config,
            response_modalities=["TEXT"]  # Явно указываем что отвечать нужно текстом
        )

        # 🔥 ДОБАВЛЕНО: asyncio.wait_for ставит жесткий лимит 60 сек
        # Если Google думает дольше - бросаем ошибку и идем к OpenAI
        response = await asyncio.wait_for(
            google_client.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=user_message,
                config=generate_config
            ),
            timeout=60.0
        )

        if response.text:
            raw_answer = response.text
        else:
            raise ValueError("Gemini вернул пустой ответ")


    except Exception as e:
        logger.warning(f"⚠️ Ошибка Gemini: {type(e).__name__}: {e}. Переключаюсь на резерв...")
        # Если fallback запрещён (глобальный поиск без фида) — GPT не поможет
        if not allow_fallback:
            logger.warning("⚠️ GPT-fallback отключён для этого запроса (нет товаров из базы)")
            return "⚠️ Сервис временно недоступен. Попробуйте повторить запрос через пару минут."

        # ---------------------------------------------------------
        # ПОПЫТКА 2: OpenAI ChatGPT (Резерв)
        # ---------------------------------------------------------

        try:
            # 🔥 Принт для понимания
            print(f"🔔 ПОПЫТКА 2: OpenAI ChatGPT (Резерв))")

            response = await openai_client.responses.create(
                model="gpt-5.1",
                instructions=system_instruction,
                input=user_message,
            )
            raw_answer = response.output_text or ""

        except Exception as ex:
            logger.critical(f"❌ CRITICAL: Все API упали: {ex}", exc_info=True)
            return "Извините, технический сбой. Повторите попытку позже."

    # --- 3. ПОСТ-ВАЛИДАЦИЯ ССЫЛОК (LEVEL 3) ---
    if raw_answer:
        # 1. 🔥 Сначала чистим от Markdown-артефактов (звездочек)
        clean_answer = clean_markdown_artifacts(raw_answer)

        # 2. Потом проверяем HTML-ссылки на валидность в уже чистом тексте
        final_answer = await validate_and_fix_links(clean_answer)

        return final_answer

    return "Не удалось получить ответ."