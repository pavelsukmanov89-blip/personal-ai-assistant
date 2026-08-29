import datetime
import json
import logging
import os
import random

import requests

logger = logging.getLogger(__name__)

MUSE_FACTS_PATH = os.path.join(os.path.dirname(__file__), "muse_facts.json")
BUFFER_FILE = os.path.join(os.path.dirname(__file__), "muse_buffer.json")
BUFFER_SIZE = 10


def _load_facts():
    try:
        with open(MUSE_FACTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"❌ [MUSE] Не удалось загрузить muse_facts.json: {e}")
        return []


def _load_buffer():
    try:
        with open(BUFFER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            # Если это старый формат — просто список, то конвертируем
            if isinstance(data, list):
                return {"last_ids": data, "last_tags": []}
    except Exception as e:
        logger.warning(f"⚠️ [MUSE] Не удалось прочитать буфер: {e}")
    return {"last_ids": [], "last_tags": []}


def _save_buffer(buffer):
    try:
        with open(BUFFER_FILE, "w", encoding="utf-8") as f:
            json.dump(buffer, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"❌ [MUSE] Не удалось сохранить буфер: {e}")


def get_random_local_fact():
    facts = _load_facts()
    if not facts:
        return ""

    buffer = _load_buffer()
    recent_ids = buffer.get("last_ids", [])
    recent_tags = buffer.get("last_tags", [])

    available = [f for f in facts if f["id"] not in recent_ids]
    if not available:
        available = facts
        recent_ids = []
        recent_tags = []

    # Анти-кластеризация: штрафуем факты с тегами, которые уже были недавно
    scored = []
    for fact in available:
        overlap = len(set(fact.get("tags", [])) & set(recent_tags))
        score = fact.get("weight", 1.0) * (0.7**overlap)
        scored.append((score, fact))

    total = sum(s for s, _ in scored)
    r = random.random() * total
    cumulative = 0
    selected = scored[-1][1]
    for score, fact in scored:
        cumulative += score
        if r <= cumulative:
            selected = fact
            break

    # Обновляем буфер
    recent_ids.append(selected["id"])
    recent_tags.extend(selected.get("tags", []))

    if len(recent_ids) > BUFFER_SIZE:
        recent_ids.pop(0)
        # обрежем и теги, чтобы не раздувались
        recent_tags = recent_tags[-50:]

    _save_buffer({"last_ids": recent_ids, "last_tags": recent_tags})

    logger.info(f"✅ [MUSE] Локальный факт выбран: {selected['id']}")
    return selected["fact"]


def get_onthisday_event():
    try:
        today = datetime.datetime.now(datetime.timezone.utc).date()
        url = (
            "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events/"
            f"{today.month}/{today.day}"
        )
        headers = {"User-Agent": "AIA/1.0 (personal assistant)"}
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            logger.warning(f"⚠️ [MUSE] Wiki статус: {response.status_code}")
            return ""

        data = response.json()
        events = data.get("events", [])
        if events:
            event = random.choice(events)
            text = event.get("text", "")
            year = event.get("year", "")
            if text:
                low_words = ["родился", "умер", "родилась", "умерла"]
                if any(word in text.lower() for word in low_words):
                    logger.info("⚠️ [MUSE] Исторический факт слабый, пропускаем")
                    return ""
                return f"{year}: {text}" if year else text
    except Exception as e:
        logger.error(f"Muse Wiki error: {e}")
    return ""


def get_muse_context():
    history_fact = get_onthisday_event()
    local_fact = get_random_local_fact()

    parts = []
    if history_fact:
        parts.append(f"Исторический факт дня: {history_fact}")
        logger.info("✅ [MUSE] Исторический факт получен")
    if local_fact:
        parts.append(f"Факт из локальной базы: {local_fact}")

    if not parts:
        logger.warning("⚠️ [MUSE] Внешний импульс не получен")

    return "\n".join(parts)
