import datetime
import logging
import math

import feedparser

# Приоритеты категорий для сортировки новостей (по убыванию важности)
PRIORITY_ORDER = {
    "demis_hassabis": 1,
    "ai": 2,
    "python": 3,
    "analytics": 4,
    "hardware": 5,
    "astronomy": 6,
    "science": 7,
    "world": 8,
    "finance": 9,
}
from dateutil import parser as dateutil_parser

from config import RSS_GROUPS
from memory_service import MemoryManager

logger = logging.getLogger(__name__)

MAX_AGE_HOURS = 24
DEDUP_THRESHOLD = 0.85


def is_fresh(published: str) -> bool:
    """Проверяет, не старше ли новость 24 часов."""
    if not published:
        return True
    try:
        pub_dt = dateutil_parser.parse(published)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - pub_dt) < datetime.timedelta(hours=MAX_AGE_HOURS)
    except Exception:
        return False


def cosine_similarity(v1, v2) -> float:
    """Вычисляет косинусное сходство между двумя векторами."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot_product = sum(x * y for x, y in zip(v1, v2))
    norm_a = math.sqrt(sum(x * x for x in v1))
    norm_b = math.sqrt(sum(x * x for x in v2))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    result = dot_product / (norm_a * norm_b)
    return result if not math.isnan(result) else 0.0


def get_latest_news(limit_per_feed=5) -> tuple[str, dict[int, str]]:
    """Собирает свежие новости с TTL, семантической дедупликацией и картой ссылок."""
    raw_news_pool = []
    news_links_map: dict[int, str] = {}
    next_id = 1
    skipped_by_age = 0
    skipped_by_semantic_duplicate = 0

    # ШАГ 1: Собираем сырые свежие новости
    for category, urls in RSS_GROUPS.items():
        for url in urls:
            try:
                feed = feedparser.parse(url)
                if not feed.entries:
                    continue

                for entry in feed.entries[:limit_per_feed]:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "")
                    published = entry.get("published", entry.get("updated", ""))
                    summary = entry.get("summary", "") or entry.get("description", "")

                    if not title:
                        continue

                    if not is_fresh(published):
                        skipped_by_age += 1
                        continue

                    raw_news_pool.append(
                        {
                            "category": category,
                            "title": title,
                            "link": link,
                            "published": published or "Дата не указана",
                            "summary": summary,
                        }
                    )

            except Exception as e:
                logger.error(f"❌ [NEWS] Ошибка при парсинге {url}: {e}")
                continue

    if not raw_news_pool:
        return (
            "НОВОСТИ ОТСУТСТВУЮТ: Все RSS-ленты заблокировали запрос или недоступны.",
            {},
        )
        # ШАГ 1.4: Жёсткая фильтрация мусора
    STOP_WORDS = [
        "спорт",
        "футбол",
        "хоккей",
        "трансфер",
        "чемпионат",
        "игра",
        "гейм",
        "кино",
        "сериал",
        "шоу-бизнес",
        "знаменитость",
        "антидепрессант",
        "медицина",
        "здоровье",
        "диета",
        "криптовалюта",
        "биткоин",
        "курс валют",
    ]

    raw_news_pool = [
        item
        for item in raw_news_pool
        if not any(
            word in (item["title"] + " " + item["summary"]).lower()
            for word in STOP_WORDS
        )
    ]
    # ШАГ 2: Семантическая дедупликация через эмбеддинги
    final_news_items = []

    memory_mgr = MemoryManager()
    texts_to_embed = [
        f"{item['title']} {item['summary']}".strip()[:800] for item in raw_news_pool
    ]

    try:
        embeddings = [memory_mgr.get_embedding(text) for text in texts_to_embed]
    except Exception as e:
        logger.error(
            f"❌ [NEWS] Ошибка получения эмбеддингов: {e}. Переключаемся на текстовую дедупликацию."
        )
        embeddings = [None] * len(raw_news_pool)

    valid_indices = []

    for i, current_item in enumerate(raw_news_pool):
        is_duplicate = False
        current_vector = embeddings[i]

        for approved_idx in valid_indices:
            if current_vector is not None and embeddings[approved_idx] is not None:
                similarity = cosine_similarity(current_vector, embeddings[approved_idx])
                if similarity >= DEDUP_THRESHOLD:
                    is_duplicate = True
                    skipped_by_semantic_duplicate += 1
                    break
            elif current_item["title"] == raw_news_pool[approved_idx]["title"]:
                is_duplicate = True
                skipped_by_semantic_duplicate += 1
                break

        if not is_duplicate:
            valid_indices.append(i)
            news_links_map[next_id] = current_item["link"]

            final_news_items.append(
                f"ID: {next_id}\n"
                f"Категория: {current_item['category']}\n"
                f"Заголовок: {current_item['title']}\n"
                f"Дата: {current_item['published']}\n"
                f"Суть: {current_item['summary']}"
            )
            next_id += 1

            if len(final_news_items) >= 10:
                break

    logger.info(f"🔍 Итого свежих уникальных новостей: {len(final_news_items)}")
    logger.info(
        f"🔍 Пропущено по возрасту: {skipped_by_age}, "
        f"по семантическим дублям: {skipped_by_semantic_duplicate}"
    )

    raw_text = "\n---\n".join(final_news_items)
    return raw_text, news_links_map


def get_news_data() -> tuple[list[dict], dict[int, str]]:
    """Возвращает список словарей и карту ссылок для передачи в LLM."""
    raw_text, news_links_map = get_latest_news()

    if not raw_text or "ОТСУТСТВУЮТ" in raw_text:
        return [], {}

    data = []
    for block in raw_text.split("\n---\n"):
        item = {}
        for line in block.split("\n"):
            if line.startswith("ID:"):
                item["id"] = line.replace("ID:", "").strip()
            elif line.startswith("Категория:"):
                item["category"] = line.replace("Категория:", "").strip()
            elif line.startswith("Заголовок:"):
                item["title"] = line.replace("Заголовок:", "").strip()
            elif line.startswith("Дата:"):
                item["date"] = line.replace("Дата:", "").strip()
            elif line.startswith("Суть:"):
                item["summary"] = line.replace("Суть:", "").strip()
        if item.get("title"):
            data.append(item)
    return data, news_links_map
