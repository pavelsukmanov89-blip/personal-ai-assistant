import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv
from requests.exceptions import ConnectionError, RequestException, Timeout

load_dotenv()

YANDEX_API_KEY = (os.getenv("YANDEX_API_KEY") or "").strip()
YANDEX_FOLDER_ID = (os.getenv("YANDEX_FOLDER_ID") or "").strip()

if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
    logger = logging.getLogger("yandex_service")
    logger.error("YANDEX_API_KEY или YANDEX_FOLDER_ID не заданы или пустые")
    sys.exit(1)

logger = logging.getLogger(__name__)

BASE_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


def _extract_text(data: dict) -> str | None:
    try:
        return data["result"]["alternatives"][0]["message"]["text"]
    except (KeyError, IndexError, TypeError):
        logger.error("Не удалось извлечь текст из ответа YandexGPT")
        return None


def ask_yandex(
    messages: list[dict[str, str]],
    model: str = "yandexgpt",
    temperature: float = 0.7,
    max_tokens: int = 2000,
    max_retries: int = 3,
    backoff_factor: float = 1.5,
    max_wait: float = 30.0,
    timeout: float = 30.0,
    max_total_time: float = 90.0,
) -> str | None:
    """
    Вызывает YandexGPT через requests с логированием и retry.
    Возвращает текст ответа или None при неудаче.
    """

    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/{model}",
        "completionOptions": {
            "temperature": temperature,
            "maxTokens": max_tokens,
        },
        "messages": messages,
    }

    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
    }

    start_time = time.time()
    attempt = 0
    last_error: Exception | None = None

    while attempt <= max_retries:
        if time.time() - start_time > max_total_time:
            logger.error("Превышено общее время запросов к YandexGPT")
            break

        try:
            logger.info(
                "Отправка запроса к YandexGPT: model=%s, messages_count=%d",
                model,
                len(messages),
            )
            response = requests.post(
                BASE_URL,
                headers=headers,
                json=payload,
                timeout=timeout,
            )

            logger.info("Ответ от YandexGPT: status=%d", response.status_code)

            if response.status_code == 200:
                data = response.json()
                result_text = _extract_text(data)
                if result_text:
                    logger.info(
                        "Успешный ответ от модели (длина: %d символов)",
                        len(result_text),
                    )
                    return result_text
                return None

            if response.status_code in (429, 500, 502, 503, 504):
                wait_time = min(backoff_factor**attempt, max_wait)
                logger.warning(
                    "Временная ошибка (status=%d). Повторим через %.1f сек. Попытка %d/%d",
                    response.status_code,
                    wait_time,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(wait_time)
                attempt += 1
                last_error = RuntimeError(f"Temporary error: {response.status_code}")
                continue

            logger.error(
                "Ошибка API (status=%d): %.300s",
                response.status_code,
                response.text,
            )
            return None

        except (Timeout, ConnectionError) as e:
            wait_time = min(backoff_factor**attempt, max_wait)
            logger.warning("Сетевая ошибка: %s. Повторим через %.1f сек.", e, wait_time)
            time.sleep(wait_time)
            attempt += 1
            last_error = e
            continue

        except RequestException as e:
            logger.error("Ошибка запроса: %s", e)
            return None
        except Exception:
            logger.exception("Неизвестная ошибка")
            return None

    logger.error("Все попытки исчерпаны. Последняя ошибка: %s", last_error)
    return None
