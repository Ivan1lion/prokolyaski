import os
import asyncio
import re
import logging
import chromadb

from typing import List, Dict, Optional, Union
from pathlib import Path
from openai import AsyncOpenAI


# Настройка логгера
logger = logging.getLogger(__name__)

# === 1. НАСТРОЙКА ПУТЕЙ И КЛИЕНТОВ ===

# Вычисляем путь к корню проекта.
# Файл лежит в: app/services/search_service.py
# .parent -> app/services
# .parent.parent -> app
# .parent.parent.parent -> корень проекта (где лежит папка chromadb_storage)
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
CHROMA_DB_PATH = os.path.join(BASE_DIR, "chromadb_storage")

# Инициализация OpenAI
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Инициализация ChromaDB
# PersistentClient = база на диске (не в оперативной памяти)
chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
collection = chroma_client.get_or_create_collection(name="strollers")

# === 2. СЛОВАРЬ-ПЕРЕВОДЧИК ===
QUIZ_TRANSLATIONS = {
    # Тип коляски
    "from_birth": "коляска для новорожденного",
    "stroller": "прогулочная коляска для детей от 6 месяцев",
    "service_only": "коляска",

    # Подтип коляски
    "stroller_folds_like_a_cane": "коляска-трость, механизм складывания по типу трость",
    "The_child's_age_is_from_6_months": "механизм складывания по типу книжка",

    # Функционал
    "2in1": "коляска 2 в 1 с люлькой",
    "3in1": "коляска 3 в 1 с автокреслом",
    "transformer": "коляска-трансформер, люлька-трансформер, люлька",

    # Сценарий
    "daily_walks": "Для ежедневных прогулок",
    "car_trips": "для удобной перевозки в автомобиле, складывание одной рукой",
    "air_travel": "для путешествий и авиаперелетов, легкая компактная для самолета ручная кладь",

    # Сезон
    "summer": "летняя с вентиляцией",
    "winter": "теплая зимняя непродуваемая (термолюлька), защита ответра",

    # Тип дороги
    "ground": "для езды по грунту, средний размер колес, хорошая амортизация",
    "asphalt": "для езды по асфальту, маневренная городская коляска, легкая",
    "ground and asphalt": "для езды как по асфальту так и по грунту, средний размер колес, хорошая амортизация",
    "offroad and snow": "для езды по бездорожью и снегу, вездеход с большими колёсами и отличной амортизацией",
}


async def get_query_embedding(text: str) -> List[float]:
    """Превращает текст запроса в вектор"""
    try:
        response = await openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=text
        )
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"Ошибка создания эмбеддинга: {e}")
        return []


def translate_quiz_to_text(quiz_data: dict) -> str:
    """Превращает JSON квиза в поисковую строку на русском."""
    search_terms = []

    for key, value in quiz_data.items():
        # 1. Если пришел СПИСОК (например: ["ground", "asphalt"])
        if isinstance(value, list):
            for item in value:
                # Пробуем перевести каждый элемент списка
                # .get(item, item) вернет перевод, если он есть, или сам item, если нет
                term = QUIZ_TRANSLATIONS.get(item, str(item))
                search_terms.append(term)

        # 2. Если пришла СТРОКА (например: "winter")
        elif isinstance(value, str):
            # Пробуем перевести строку
            term = QUIZ_TRANSLATIONS.get(value, value)
            search_terms.append(term)

        # 3. (Опционально) Если перевода значения нет, но есть перевод ключа
        elif key in QUIZ_TRANSLATIONS:
            search_terms.append(QUIZ_TRANSLATIONS[key])

    return " ".join(search_terms)


async def search_products(
        user_query: str,
        quiz_json: Optional[dict] = None,
        # 🔥 Теперь принимаем: один ID (int), список ID (List[int]) или None
        allowed_magazine_ids: Union[int, List[int], None] = None,
        top_k: int = 10
) -> str:
    """
    Главная функция поиска (Адаптировано под ChromaDB + Защита от дублей).
    """

    # 1. Формируем "Идеальный поисковый запрос"
    full_search_text = user_query
    if quiz_json:
        translated_quiz = translate_quiz_to_text(quiz_json)
        full_search_text = f"{full_search_text} {translated_quiz}"

    logger.info(f"🔎 Ищем в ChromaDB по фразе: '{full_search_text}' (IDs: {allowed_magazine_ids})")

    # 2. Получаем вектор
    vector = await get_query_embedding(full_search_text)
    if not vector:
        return ""

    try:
        # 3. Запрос в базу ChromaDB
        # 🔥 Берем в 4 раза больше, так как будем фильтровать дубли и чужие магазины
        fetch_multiplier = 4
        fetch_k = int(top_k * fetch_multiplier)

        # 🔥🔥🔥 Мы говорим Python: "Выполни эту тяжелую функцию в отдельном потоке,
        # а мы пока подождем (await), но других юзеров блокировать не будем".
        def run_query():
            return collection.query(
                query_embeddings=[vector],
                n_results=fetch_k
            )

        results = await asyncio.to_thread(run_query)
        # 🔥🔥🔥 КОНЕЦ 🔥🔥🔥

        # Chroma возвращает структуру: {'ids': [[]], 'metadatas': [[]], 'distances': [[]]}
        # Проверяем, есть ли результаты
        if not results['ids'] or not results['ids'][0]:
            return ""

        # 4. Обработка результатов и фильтрация
        context_text = ""
        found_count = 0

        # 🔥 Множество для запоминания уже найденных названий (анти-дубль)
        seen_names = set()

        # Данные первого (и единственного) запроса
        metadatas_list = results['metadatas'][0]
        distances_list = results['distances'][0]  # Чем меньше, тем лучше

        # --- ПОДГОТОВКА СПИСКА РАЗРЕШЕННЫХ ID ---
        target_ids_set = set()
        if allowed_magazine_ids is not None:
            if isinstance(allowed_magazine_ids, int):
                target_ids_set.add(str(allowed_magazine_ids))
            else:
                # Если передан список [1, 5], превращаем в set строчек {"1", "5"}
                target_ids_set = set(str(x) for x in allowed_magazine_ids)

        for i, meta in enumerate(metadatas_list):
            if found_count >= top_k:
                break

            # --- 🔥 НОВАЯ ФИЛЬТРАЦИЯ (Один или Список) ---
            # В update_vectors мы сохраняли "magazine_ids_str": "1,2,5"
            if target_ids_set:
                owners_str = meta.get("magazine_ids_str", "")
                owners_list = owners_str.split(",")

                # Проверяем пересечение: есть ли хоть один наш ID среди владельцев?
                # Если пересечения нет (множество пустое) -> пропускаем
                if not (set(owners_list) & target_ids_set):
                    continue

            # Получаем данные
            name = meta.get('name', 'Без названия')

            # --- 🔥 ЗАЩИТА ОТ ДУБЛЕЙ ---
            # Нормализация: убираем спецсимволы, пробелы, приводим к нижнему регистру
            clean_name = re.sub(r'[^\w\s]', '', name).lower().strip()

            if clean_name in seen_names:
                continue  # Пропускаем, если такое название уже было

            seen_names.add(clean_name)  # Запоминаем

            # --- СБОРКА ОТВЕТА ---
            price = meta.get('price', 'Цена не указана')
            url = meta.get('url', '#')
            # Обрезаем описание
            desc = meta.get('description', '')[:3000]

            # 🔥 ВОССТАНОВЛЕНА РЕЛЕВАНТНОСТЬ 🔥
            # В Chroma чем меньше distance, тем лучше (0 = копия).
            # Превращаем в % схожести: (1 - distance) * 100
            dist = distances_list[i]
            # Защита от отрицательных чисел (если векторы странные), хотя обычно distance <= 1
            similarity = max(0.0, 1.0 - dist)
            relevance_percent = int(similarity * 100)

            context_text += (
                f"- <b>{name}</b>\n"
                f"  Цена: {price} руб.\n"
                f"  Ссылка: {url}\n"
                f"  Описание: {desc}...\n"
                f"  <i>(Релевантность: {relevance_percent}%)</i>\n\n"
            )
            found_count += 1

        return context_text

    except Exception as e:
        logger.error(f"Ошибка поиска в ChromaDB: {e}")
        return ""
