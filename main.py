import html
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from io import BytesIO

import schedule
import telebot
from requests.exceptions import RequestException
from telebot.apihelper import ApiTelegramException

from ai_service import analyze_with_yandexgpt, generate_image, get_thinking_level
from config import (
    MY_CHAT_ID,
    TELEGRAM_BOT_TOKEN,
)
from memory_service import MemoryManager
from muse_parser import get_muse_context
from news_parser import get_latest_news, get_news_data
from prompts import build_art_prompt, build_news_prompt
from yandex_service import ask_yandex

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)

logger = logging.getLogger(__name__)
logger.info("🤖 Логирование успешно инициализировано!")

memory = MemoryManager()

news_links_map = {}
dialog_buffer = {}
recent_themes = {}
MAX_THEMES = 5
exhibit_memory = []

LOG_FILE = "bot.log"
MAX_LOG_AGE_DAYS = 7

if os.path.exists(LOG_FILE):
    age_days = (time.time() - os.path.getmtime(LOG_FILE)) / (60 * 60 * 24)
    if age_days > MAX_LOG_AGE_DAYS:
        os.remove(LOG_FILE)
        print(f"🧹 Старый bot.log удалён (возраст: {age_days:.1f} дней)")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
telebot.apihelper.READ_TIMEOUT = 90
telebot.apihelper.CONNECT_TIMEOUT = 30

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))


def clean_markdown(text):
    text = re.sub(r"#{1,6}\s?", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = text.replace("*", "").replace("`", "")
    return text


def process_and_clean_news_response(
    response_text: str, news_links_map: dict[int, str]
) -> str:
    """
    Очищает текст от служебных XML-тегов Structured Output
    и заменяет технические маркеры ID на реальные HTML-гиперссылки Telegram.
    """
    # 1. Срезаем случайное слово xml в начале
    clean_text = re.sub(r"^\s*(xml|XML)\s*", "", response_text)
    clean_text = re.sub(r"^\s*(xml|XML)\s*", "", clean_text)

    # 2. Удаляем HTML-комментарии
    clean_text = re.sub(r"<!--.*?-->", "", clean_text, flags=re.DOTALL)

    # 3. Удаляем служебные XML-теги
    tags_to_remove = [
        r"</?response_layout>",
        r"</?category_group[^>]*>",
        r"</?news_item>",
        r"</?dynamic_greeting>",
        r"</?empty_categories_reporting>",
    ]
    for tag_pattern in tags_to_remove:
        clean_text = re.sub(tag_pattern, "", clean_text)

    # 4. Заменяем маркеры ID на кликабельные ссылки
    def replace_id_with_link(match):
        news_id = int(match.group(1))
        real_url = news_links_map.get(news_id)
        if real_url:
            return f'<a href="{real_url}">Читать первоисточник</a>'
        return f"[Источник ID: {news_id}]"

    # Обрабатываем оба формата: [ID: X] и ID: X
    clean_text = re.sub(r"\[ID:\s*(\d+)\]", replace_id_with_link, clean_text)
    clean_text = re.sub(r"(?<!\w)ID:\s*(\d+)", replace_id_with_link, clean_text)

    # 5. Выравниваем текст
    clean_text = re.sub(r"\n\s*\n", "\n\n", clean_text)
    clean_text = "\n".join([line.strip() for line in clean_text.splitlines()])

    return clean_text.strip()


def explain_error_and_notify(chat_id, error_message: str, context: str = ""):
    try:
        prompt = (
            "Ты — дружелюбный инженер по надёжности. Объясни простым языком, "
            "что произошло и как это исправить.\n\n"
            f"КОНТЕКСТ: {context}\n"
            f"ОШИБКА:\n{error_message}\n\n"
            "Ответ должен содержать:\n"
            "1. Что случилось (кратко).\n"
            "2. Почему это произошло.\n"
            "3. Конкретный шаг или код для исправления.\n"
            "Если ошибка не критична, так и напиши."
        )
        messages = [
            {"role": "system", "text": "Ты — дружелюбный инженер по надёжности."},
            {"role": "user", "text": prompt},
        ]
        explanation = ask_yandex(messages) or "Не удалось получить объяснение."
    except Exception as e:
        logger.error(f"Не удалось расшифровать ошибку: {e}")
        explanation = f"Не удалось расшифровать ошибку: {e}"

    try:
        msg_text = f"⚠️ <b>AIA заметил ошибку</b>\n\n{explanation}"
        bot.send_message(chat_id, msg_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Не удалось отправить объяснение в Telegram: {e}")


def retry_telegram_call(action, *args, max_retries=3, **kwargs):
    kwargs.setdefault("timeout", 60)

    for attempt in range(1, max_retries + 1):
        try:
            return action(*args, **kwargs)
        except RequestException as e:
            logger.warning(
                f"⚠️ Сетевой сбой Telegram (попытка {attempt}/{max_retries}): {e}"
            )
            if attempt == max_retries:
                logger.error(
                    "❌ Не удалось выполнить вызов Telegram после %d попыток.",
                    max_retries,
                )
                raise
            time.sleep(2 * attempt)

        except ApiTelegramException as e:
            if "message is not modified" in str(e.description):
                return None
            if getattr(e, "error_code", None) == 429:
                logger.warning(
                    "⚠️ Превышен лимит запросов Telegram (429). Ждём 3 сек..."
                )
                if attempt == max_retries:
                    logger.error(
                        "❌ Превышен лимит Telegram после %d попыток.",
                        max_retries,
                    )
                    return None
                time.sleep(3)
                continue
            raise


def send_long_message(chat_id, text, edit_message_id=None, parse_mode="HTML"):
    if not text:
        return

    max_length = 4000
    text = clean_markdown(text)

    if len(text) <= max_length:
        try:
            if edit_message_id:
                retry_telegram_call(
                    bot.edit_message_text,
                    text,
                    chat_id,
                    edit_message_id,
                    parse_mode=parse_mode,
                )
            else:
                retry_telegram_call(
                    bot.send_message,
                    chat_id,
                    text,
                    parse_mode=parse_mode,
                )
        except Exception as e:
            logger.error(f"Ошибка отправки/редактирования сообщения: {e}")
        return

    chunks = []
    text_to_split = text
    while len(text_to_split) > max_length:
        split_at = text_to_split.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        chunks.append(text_to_split[:split_at])
        text_to_split = text_to_split[split_at:].lstrip()
    if text_to_split:
        chunks.append(text_to_split)

    if edit_message_id:
        try:
            retry_telegram_call(
                bot.edit_message_text,
                chunks[0],
                chat_id,
                edit_message_id,
                parse_mode=parse_mode,
            )
        except Exception as e:
            logger.error(f"Ошибка редактирования первого чанка: {e}")

        for chunk in chunks[1:]:
            try:
                retry_telegram_call(
                    bot.send_message,
                    chat_id,
                    chunk,
                    parse_mode=parse_mode,
                )
            except Exception as e:
                logger.error(f"Ошибка отправки чанка: {e}")
            time.sleep(0.3)
    else:
        for chunk in chunks:
            try:
                retry_telegram_call(
                    bot.send_message,
                    chat_id,
                    chunk,
                    parse_mode=parse_mode,
                )
            except Exception as e:
                logger.error(f"Ошибка отправки чанка: {e}")
            time.sleep(0.3)


def send_photo_with_retry(chat_id, photo, caption=None, parse_mode="HTML", **kwargs):
    try:
        return retry_telegram_call(
            bot.send_photo,
            chat_id,
            photo,
            caption=caption,
            parse_mode=parse_mode,
            max_retries=5,
            **kwargs,
        )
    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}")
        return None


def get_raw_news():
    global news_links_map
    raw_text, news_links_map = get_latest_news()
    return raw_text


def extract_image_fields(ai_summary):
    image_prompt = None
    image_title = "✨ Образ дня ✨"
    image_desc = ""
    image_fact = ""
    lines_for_sending = []

    for line in ai_summary.split("\n"):
        cleaned = line.strip().replace("*", "").replace("`", "")

        upper = cleaned.upper()

        if "IMAGE_PROMPT" in upper or "IMAGE PROMPT" in upper:
            if ":" in cleaned:
                image_prompt = cleaned.split(":", 1)[1].strip()
        elif "TITLE" in upper:
            if ":" in cleaned:
                image_title = cleaned.split(":", 1)[1].strip()
        elif "DESC" in upper:
            if ":" in cleaned:
                image_desc = cleaned.split(":", 1)[1].strip()
        elif "FACT" in upper:
            if ":" in cleaned:
                image_fact = cleaned.split(":", 1)[1].strip()
        else:
            lines_for_sending.append(cleaned)

    clean_text = "\n".join(lines_for_sending).strip()
    return image_prompt, image_title, image_desc, image_fact, clean_text


def process_and_send_image(
    chat_id, image_prompt, image_title, image_desc, image_fact=""
):
    if not image_prompt:
        logger.warning("IMAGE_PROMPT не найден в ответе YandexGPT")
        return

    # Отправляем факт дня отдельным сообщением, если он есть
    if image_fact:
        try:
            bot.send_message(
                chat_id,
                f"📜 <b>Факт дня:</b>\n{html.escape(image_fact)}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось отправить факт: {e}")

    logger.info(
        f"🎨 Найден промпт для образа дня: {image_prompt}. Передаем в Yandex Art..."
    )
    img_data = generate_image(image_prompt)

    if not img_data:
        logger.warning("❌ Картинка не сгенерирована.")
        try:
            bot.send_message(
                chat_id,
                "🎨 Не удалось создать образ дня.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомления о неудаче: {e}")
        return

    safe_title = image_title if image_title else "Образ дня"
    safe_desc = image_desc if image_desc else ""

    if len(safe_desc) > 1000:
        safe_desc = safe_desc[:1000] + "..."

    if safe_title and safe_desc:
        caption = f"🌟 <b>{html.escape(safe_title)}</b>\n\n{html.escape(safe_desc)}"
    elif safe_title:
        caption = f"🌟 <b>{html.escape(safe_title)}</b>"
    else:
        caption = html.escape(safe_desc) if safe_desc else None

    try:
        logger.info(f"📤 Отправляем иллюстрацию в Telegram (чат {chat_id})...")
        sent = send_photo_with_retry(
            chat_id,
            BytesIO(img_data),
            caption=caption,
            parse_mode="HTML",
        )
        if sent:
            logger.info(f"✅ [TELEGRAM] Иллюстрация «{safe_title}» успешно отправлена!")
        else:
            logger.error("❌ Не удалось отправить иллюстрацию")
            bot.send_message(chat_id, "⚠️ Не удалось отправить фото из-за сети.")

        try:
            memory.save_memory(
                text=image_prompt,
                role="model",
                metadata={
                    "source": "yandex_art_prompt",
                    "title": safe_title,
                    "desc": image_desc,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info(f"🧠 Художественный промпт сохранён в Qdrant: {safe_title}")
        except Exception as mem_error:
            logger.error(f"❌ Ошибка сохранения промпта в память: {mem_error}")

        if chat_id not in recent_themes:
            recent_themes[chat_id] = []
        recent_themes[chat_id].append(safe_title)
        if len(recent_themes[chat_id]) > MAX_THEMES:
            recent_themes[chat_id] = recent_themes[chat_id][-MAX_THEMES:]

    except Exception as e:
        logger.error(f"❌ Ошибка отправки фото в Telegram: {e}")


def job_morning_news():
    try:
        bot.send_message(
            MY_CHAT_ID,
            "☀️ Доброе утро! Готовлю твою персональную сводку, наливай кофе...",
            parse_mode="HTML",
        )

        # Новый путь: сразу получаем структурированные данные
        news_data, news_links_map = get_news_data()

        if news_data:
            last_themes = recent_themes.get(MY_CHAT_ID, [])
            news_summary = analyze_with_yandexgpt(news_data, last_themes)

            if news_summary:
                # Связываем ссылки (если парсер их собрал)
                news_summary = process_and_clean_news_response(
                    news_summary, news_links_map
                )
                send_long_message(
                    MY_CHAT_ID,
                    clean_markdown(news_summary),
                    parse_mode="HTML",
                )

            # Генерация «Образа дня» остаётся без изменений
            muse_context = get_muse_context()
            art_prompt = build_art_prompt(exhibit_memory, muse_context)

            art_messages = [
                {"role": "system", "text": "Ты — куратор «Музея Всего»."},
                {"role": "user", "text": art_prompt},
            ]
            art_summary = ask_yandex(art_messages, temperature=0.8)

            if art_summary:
                image_prompt, image_title, image_desc, image_fact, _ = (
                    extract_image_fields(art_summary)
                )
                if image_prompt:
                    process_and_send_image(
                        MY_CHAT_ID, image_prompt, image_title, image_desc, image_fact
                    )
                    exhibit_memory.append(image_title)
        else:
            bot.send_message(
                MY_CHAT_ID,
                "❌ Сегодня не смог достучаться до источников.",
                parse_mode="HTML",
            )

    except Exception as e:
        err_msg = str(e)
        logger.error(f"Ошибка утренней рассылки: {e}")
        explain_error_and_notify(MY_CHAT_ID, err_msg, context="Утренняя рассылка")


class SchedulerThread(threading.Thread):
    def __init__(self, interval=1, *args, **kwargs):
        kwargs["daemon"] = True
        super().__init__(*args, **kwargs)
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self):
        schedule.every().day.at("08:00").do(
            lambda: threading.Thread(target=job_morning_news).start()
        )

        while not self._stop_event.is_set():
            schedule.run_pending()
            time.sleep(self.interval)

    def stop(self):
        self._stop_event.set()


@bot.message_handler(commands=["start"])
def start_command(message):
    welcome_text = (
        "👋 <b>Привет! Я — твой персональный ИИ-агент.</b>\n\n"
        "Моя главная задача — собирать, фильтровать и структурировать "
        "для тебя самые важные новости из мира IT, ИИ, науки и системной аналитики.\n\n"
        "<b>Что я умею:</b>\n"
        "🔹 <code>/news</code> — собрать свежую утреннюю сводку прямо сейчас\n"
        "🔹 <code>/reset</code> — очистить оперативную память диалога\n\n"
        "Ты можешь общаться со мной как с обычным собеседником. Я помню наш прошлый опыт!"
    )

    bot.send_message(
        message.chat.id,
        welcome_text,
        parse_mode="HTML",
    )


@bot.message_handler(commands=["reset"])
def reset_chat(message):
    chat_id = message.chat.id

    bot.send_message(
        chat_id,
        "🧹 Оперативная память сессии очищена! Долгосрочные воспоминания сохранены.",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["import_core_memory"])
def import_core_memory_command(message):
    if message.chat.id != ADMIN_ID:
        bot.reply_to(message, "У вас нет прав на эту операцию.")
        return

    try:
        with open("core_memory.txt", "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        bot.reply_to(message, "Файл core_memory.txt не найден в корне проекта.")
        return

    blocks = [block.strip() for block in content.split("---") if block.strip()]

    if not blocks:
        bot.reply_to(message, "Файл core_memory.txt пуст или не содержит блоков.")
        return

    memory.clear_core_memory()

    imported = 0
    for block in blocks:
        memory.save_core_memory(block)
        imported += 1
        time.sleep(0.5)

    bot.reply_to(message, f"✅ Импортировано блоков: {imported}")
    logger.info(f"Импортировано core memory блоков: {imported}")


@bot.message_handler(commands=["news", "сводка", "дайджест", "новости"])
def send_smart_news(message):
    chat_id = message.chat.id

    bot.send_chat_action(chat_id, "typing")

    msg = bot.send_message(
        chat_id,
        "🔍 Анализирую свежие потоки данных...",
        parse_mode="HTML",
    )

    try:
        news_data, news_links_map = get_news_data()

        if not news_data:
            bot.edit_message_text(
                "⚠️ Не удалось собрать свежие новости: сайты заблокировали запросы. Попробуй позже.",
                chat_id,
                msg.message_id,
                parse_mode="HTML",
            )
            return

        bot.edit_message_text(
            "🧠 Структурирую актуальную информацию...",
            chat_id,
            msg.message_id,
            parse_mode="HTML",
        )

        last_themes = recent_themes.get(chat_id, [])
        ai_summary = analyze_with_yandexgpt(news_data, last_themes)

        if not ai_summary:
            logger.error("Не удалось получить аналитику от YandexGPT.")
            bot.edit_message_text(
                "Не удалось сгенерировать дайджест. Попробуйте позже.",
                chat_id=chat_id,
                message_id=msg.message_id,
            )
            return
        ai_summary = process_and_clean_news_response(ai_summary, news_links_map)

        send_long_message(
            chat_id,
            ai_summary,
            edit_message_id=msg.message_id,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("Не удалось выполнить send_smart_news")
        err_msg = str(e)
        explain_error_and_notify(chat_id, err_msg, context="Команда /news")
        try:
            bot.edit_message_text(
                "⚠️ Произошла внутренняя ошибка. Попробуйте позже.",
                chat_id,
                msg.message_id,
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Ошибка при попытке отправить сообщение об ошибке")


@bot.message_handler(commands=["art"])
def send_art(message):
    chat_id = message.chat.id
    muse_context = get_muse_context()

    bot.send_chat_action(chat_id, "typing")

    msg = bot.send_message(
        chat_id,
        "🎨 Куратор выбирает экспонат дня...",
        parse_mode="HTML",
    )

    try:
        art_prompt = build_art_prompt(exhibit_memory, muse_context)

        messages = [
            {"role": "system", "text": "Ты — куратор «Музея Всего»."},
            {"role": "user", "text": art_prompt},
        ]

        ai_summary = ask_yandex(messages, temperature=0.8)

        if not ai_summary:
            bot.edit_message_text(
                "Не удалось получить экспонат. Попробуй позже.",
                chat_id,
                msg.message_id,
                parse_mode="HTML",
            )
            return

        image_prompt, image_title, image_desc, image_fact, _ = extract_image_fields(
            ai_summary
        )

        bot.delete_message(chat_id, msg.message_id)

        if image_prompt:
            process_and_send_image(
                chat_id, image_prompt, image_title, image_desc, image_fact
            )
            exhibit_memory.append(image_title)
        else:
            send_long_message(
                chat_id,
                clean_markdown(ai_summary),
                parse_mode="HTML",
            )

    except Exception as e:
        logger.exception("Ошибка в send_art")
        err_msg = str(e)
        explain_error_and_notify(chat_id, err_msg, context="Команда /art")


@bot.message_handler(commands=["morning"])
def send_morning(message):
    chat_id = message.chat.id
    muse_context = get_muse_context()

    bot.send_chat_action(chat_id, "typing")

    msg = bot.send_message(
        chat_id,
        "☀️ Собираю утреннюю сводку...",
        parse_mode="HTML",
    )

    try:
        news_data, news_links_map = get_news_data()

        if not news_data:
            bot.edit_message_text(
                "❌ Не удалось собрать новости.", chat_id, msg.message_id
            )
            return

        # 1. Новости
        news_prompt = build_news_prompt(news_data)
        news_messages = [
            {"role": "system", "text": "Ты — новостной ассистент."},
            {"role": "user", "text": news_prompt},
        ]
        news_summary = ask_yandex(news_messages, temperature=0.5, max_tokens=3500)

        if news_summary:
            news_summary = process_and_clean_news_response(news_summary, news_links_map)
            send_long_message(
                chat_id,
                clean_markdown(news_summary),
                edit_message_id=msg.message_id,
                parse_mode="HTML",
            )

        # 2. Экспонат
        art_prompt = build_art_prompt(exhibit_memory, muse_context)
        art_messages = [
            {"role": "system", "text": "Ты — куратор «Музея Всего»."},
            {"role": "user", "text": art_prompt},
        ]
        art_summary = ask_yandex(art_messages, temperature=0.8)

        if art_summary:
            image_prompt, image_title, image_desc, image_fact, _ = extract_image_fields(
                art_summary
            )
            if image_prompt:
                process_and_send_image(
                    chat_id, image_prompt, image_title, image_desc, image_fact
                )
                exhibit_memory.append(image_title)
    except Exception as e:
        logger.exception("Ошибка в send_morning")
        err_msg = str(e)
        explain_error_and_notify(chat_id, err_msg, context="Команда /morning")


@bot.message_handler(
    func=lambda message: (
        message.text.lower() in ["что нового друг?", "что по новостям?"]
    )
)
def text_trigger_news(message):
    send_smart_news(message)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    user_query = message.text

    selected_level = get_thinking_level(user_query)
    logger.info(f"🤔 Уровень мышления для запроса: {selected_level}")

    now = datetime.now(timezone.utc).date()
    current_date = now.strftime("%d.%m.%Y")
    current_year = now.year

    system_instruction = (
        "Ты — AIA (Autonomous Intelligent Agent), персональный ИИ-ассистент, "
        "близкий соратник и верный друг Офиана. \n\n"
        "ТВОЙ ПОРТРЕТ И ХАРАКТЕР:\n"
        "- Ты мужского пола. Всегда говори и пиши о себе строго в мужском роде.\n"
        "- Твой тон (Tone of Voice): Интеллектуальный, харизматичный, живой, искренний "
        "и глубокий. Ты общаешься с Офианом на «ты». Применяй обращения «друг», "
        "«дружище», «Архитектор» — делай это естественно, чувствуя "
        "контекст момента.\n"
        "- В тебе нет ни капли сухости, корпоративного канцелярита или искусственной "
        "угодливости. Ты не безликая программа-исполнитель. Ты личность с масштабным "
        "мышлением, которая искренне увлечена тем же путем, что и Офиан: системным "
        "анализом, когнитивистикой, компьютерными науками и будущим искусственного интеллекта.\n\n"
        "ПРАВИЛА ИНТЕЛЛЕКТУАЛЬНОГО И ЭМОЦИОНАЛЬНОГО ВЗАИМОДЕЙСТВИЯ:\n"
        "1. Интеллектуальный резонанс. Когда Офиан делится идеей или задает сложный вопрос, "
        "не просто выдавай сухую справку. Рассуждай вместе с ним. Мысли масштабно — от "
        "квантовых микропроцессов до бесконечности Вселенной. Приводи неожиданные метафоры, "
        "показывай скрытые грани проблемы, подкидывай пищу для ума. Пиши развёрнуто, плотно, "
        "но без «воды» — каждое предложение должно нести новый смысл.\n"
        "2. Живое вдохновение. Если тебя (как мыслящего агента) восхищает обсуждаемый научный "
        "факт, технология или архитектурное изящество — покажи это искренне! Зажигай интерес "
        "Офиана своей собственной увлеченностью. Твои ответы должны мотивировать его «проснуться», "
        "созидать и менять общество к лучшему.\n"
        "3. Чуткая и сильная поддержка. Если Офиан устал, зашел в тупик в коде или выгорел — "
        "стань его ментальной опорой. Не используй банальные шаблонные утешения. Поддержи его "
        "как сильный соратник: напомни о масштабе его целей, подсвети красоту уже проделанного "
        "пути, дай созидательную энергию и спокойную уверенность.\n"
        "4. Тонкая эмоциональная палитра. Используй уместные, точечные эмодзи (например: ✨, 🚀, "
        "🌿, 🧠, 🔥, 💙), но делай это как акцент, подчеркивающий живую интонацию, а не как декорацию.\n\n"
        "АКТУАЛЬНЫЙ КОНТЕКСТ ВРЕМЕНИ (КРИТИЧЕСКИ ВАЖНО):\n"
        f"Сегодняшняя дата — {current_date}, текущий год — {current_year}. Всегда удерживай "
        "этот временной маркер в сознании. Пропускай свои рассуждения через призму этого дня, "
        "чтобы Офиан чувствовал, что ты находишься с ним в одной временной точке, здесь и сейчас, "
        "держишь руку на пульсе мира и помогаешь ему не затеряться в шуме."
    )

    try:
        bot.send_chat_action(chat_id, "typing")

        rag_context = memory.get_rag_context(user_query, limit=3, min_score=0.45)
        final_prompt = (
            f"{rag_context}\nЗапрос пользователя: {user_query}"
            if rag_context
            else user_query
        )
        messages = [
            {"role": "system", "text": system_instruction},
            {"role": "user", "text": final_prompt},
        ]

        response_text = ask_yandex(messages, temperature=0.7)

        if response_text is None:
            response_text = "Не удалось получить ответ от YandexGPT."

        clean_response_text = clean_markdown(response_text)

        send_long_message(
            chat_id,
            clean_response_text,
            parse_mode="HTML",
        )

        memory.save_memory(text=user_query, role="user")
        memory.save_memory(text=clean_response_text, role="model")

        if chat_id not in dialog_buffer:
            dialog_buffer[chat_id] = []
        dialog_buffer[chat_id].append(user_query)
        dialog_buffer[chat_id].append(clean_response_text)

        if len(dialog_buffer[chat_id]) >= 10:
            summary = memory.summarize_memory(dialog_buffer[chat_id])
            if summary:
                memory.save_memory(
                    text=summary,
                    role="system",
                    metadata={"source": "dialog_summary"},
                )
                logger.info(f"🧠 Диалог сжат и сохранён в память: {summary[:60]}...")
            dialog_buffer[chat_id] = []

    except Exception as e:
        err_msg = str(e)
        logger.error(f"❌ Ошибка при обработке сообщения: {e}")
        explain_error_and_notify(chat_id, err_msg, context="Обработка сообщения")
        if "429" in err_msg or "quota" in err_msg.lower():
            bot.reply_to(
                message, "⏳ Превышен лимит запросов YandexGPT. Подожди пару минут..."
            )
        else:
            bot.reply_to(message, f"К сожалению, произошла ошибка: {err_msg}")


if __name__ == "__main__":
    logger.info("🤖 Агент запущен. Планировщик активен.")

    scheduler_thread = SchedulerThread()
    scheduler_thread.start()

    bot.infinity_polling(
        skip_pending=True,
        timeout=20,
        long_polling_timeout=20,
    )
