import base64
import binascii
import logging
import os
import time

import requests
from dotenv import load_dotenv

from prompts import build_news_prompt, build_system_instruction
from yandex_service import ask_yandex

# Инициализация клиента
load_dotenv()
YANDEX_ART_API_KEY = (os.getenv("YANDEX_ART_API_KEY") or "").strip()
YANDEX_FOLDER_ID = (os.getenv("YANDEX_FOLDER_ID") or "").strip()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =====================================================================
# ОСНОВНОЙ ФУНКЦИОНАЛ
# =====================================================================


def get_thinking_level(user_prompt: str) -> str:
    """Автоматически выбирает уровень мышления на основе текста запроса."""
    if not isinstance(user_prompt, str) or not user_prompt.strip():
        return "LOW"

    prompt_lower = user_prompt.lower()

    # Темы, которые точно требуют глубокого размышления (HIGH)
    high_topics = [
        "вселенная",
        "космос",
        "философия",
        "смысл",
        "сознание",
        "жизнь",
        "разум",
        "искусство",
        "природа",
        "будущее",
        "эволюция",
        "технология",
        "наука",
        "квант",
        "физика",
        "математика",
        "мозг",
        "нейро",
        "душа",
        "этика",
        "смерть",
        "время",
        "бесконечность",
    ]

    high_priority = [
        "код",
        "скрипт",
        "алгоритм",
        "ошибка",
        "bug",
        "докажи",
        "оптимизируй",
        "архитектура",
        "проанализируй",
        "разбери",
        "спроектируй",
    ]

    if (
        any(word in prompt_lower for word in high_topics)
        or any(word in prompt_lower for word in high_priority)
        or len(user_prompt) > 500
    ):
        return "HIGH"

    medium_priority = [
        "план",
        "сравни",
        "почему",
        "как",
        "объясни",
        "расскажи",
        "помоги",
    ]
    if any(word in prompt_lower for word in medium_priority):
        return "MEDIUM"

    return "LOW"


def analyze_with_yandexgpt(news_data, last_themes=None):
    """Анализ новостей и генерация дайджеста."""
    if not news_data:
        logger.warning("⚠️ Пустой news_data передан в analyze_with_yandexgpt")
        return ""

    system_instruction = build_system_instruction()
    final_prompt = build_news_prompt(news_data)

    messages = [
        {"role": "system", "text": system_instruction},
        {"role": "user", "text": final_prompt},
    ]

    try:
        answer = ask_yandex(messages, temperature=0.5, max_tokens=3500)
        return answer or ""
    except Exception as e:
        logger.error("❌ Ошибка в analyze_with_yandexgpt: %s", e)
        return ""


def generate_image(prompt_text):
    """Генерация изображения через Yandex Art."""
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        logger.warning("⚠️ Пустой промпт для генерации изображения")
        return None

    api_key = YANDEX_ART_API_KEY
    folder_id = YANDEX_FOLDER_ID

    if not api_key or not folder_id:
        logger.error(
            "❌ [YANDEX_ART] YANDEX_ART_API_KEY или YANDEX_FOLDER_ID не заданы в .env"
        )
        return None

    safe_prompt = prompt_text.strip()[:490]
    start_time = time.time()

    logger.info("🎨 [YANDEX_ART] Запуск процесса генерации изображения...")
    logger.info("📝 [YANDEX_ART] Длина промпта: %d символов", len(safe_prompt))
    logger.info('📝 [YANDEX_ART] Промпт: "%.100s..."', safe_prompt)

    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
    headers = {
        "Authorization": f"Api-Key {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "modelUri": f"art://{folder_id}/yandex-art/latest",
        "generationOptions": {
            "seed": 1863,
            "mimeType": "image/jpeg",
            "aspectRatio": {"widthRatio": "1", "heightRatio": "1"},
        },
        "messages": [{"weight": 1, "text": safe_prompt}],
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)

        if response.status_code != 200:
            logger.error(
                "❌ [YANDEX_ART] Ошибка API %s: %.300s",
                response.status_code,
                response.text,
            )
            return None

        operation_id = response.json().get("id")
        if not operation_id:
            logger.error("❌ [YANDEX_ART] Не получен ID операции генерации")
            return None

        logger.info(
            "⏳ [YANDEX_ART] Запрос принят Яндексом. ID операции: %s. Ожидаем готовности...",
            operation_id,
        )

        status_url = (
            f"https://operation.api.cloud.yandex.net:443/operations/{operation_id}"
        )

        consecutive_errors = 0

        for _ in range(30):
            time.sleep(2)
            try:
                status_response = requests.get(status_url, headers=headers, timeout=10)
            except Exception as e:
                consecutive_errors += 1
                logger.warning("⚠️ Сетевая ошибка при проверке статуса: %s", e)
                if consecutive_errors >= 3:
                    logger.error("❌ Слишком много сетевых ошибок. Прерываю ожидание.")
                    return None
                continue

            if status_response.status_code != 200:
                consecutive_errors += 1
                logger.error(
                    "❌ [YANDEX_ART] Ошибка проверки статуса %s: %.200s",
                    status_response.status_code,
                    status_response.text,
                )
                if consecutive_errors >= 3:
                    logger.error("❌ Много ошибок статуса. Прерываю ожидание.")
                    return None
                continue

            consecutive_errors = 0
            status_data = status_response.json()

            if not status_data.get("done"):
                continue

            if status_data.get("error"):
                logger.error(
                    "❌ [YANDEX_ART] Генерация завершилась ошибкой: %.300s",
                    str(status_data["error"]),
                )
                return None

            image_base64 = status_data.get("response", {}).get("image")
            if not image_base64:
                logger.error(
                    "❌ [YANDEX_ART] В ответе Yandex Art отсутствует изображение"
                )
                return None

            try:
                image_bytes = base64.b64decode(image_base64)
            except (binascii.Error, ValueError) as e:
                logger.error("❌ [YANDEX_ART] Невалидный base64: %s", e)
                return None

            elapsed = round(time.time() - start_time, 2)
            logger.info(
                "🖼 [YANDEX_ART] Изображение успешно сгенерировано! Время выполнения: %s сек.",
                elapsed,
            )
            return image_bytes

        logger.error("❌ [YANDEX_ART] Превышено время ожидания генерации изображения")
        return None

    except Exception as error:
        logger.error("❌ [YANDEX_ART] Ошибка при генерации изображения: %s", error)
        return None
