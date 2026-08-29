import ast
import logging
import os
import re
import subprocess
import sys
import time

import telebot
from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from yandex_service import ask_yandex

load_dotenv()

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
MY_CHAT_ID = (os.getenv("MY_CHAT_ID") or "").strip()

notifier_bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

ENTRY_SCRIPT = "main.py"
MAX_RETRY_ATTEMPTS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [WATCHDOG] - %(message)s",
)
logger = logging.getLogger(__name__)


class QualityCheckHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith(".py"):
            print(f"🔍 [WATCHDOG] Проверяем качество кода в {event.src_path}...")

            result = subprocess.run(
                ["ruff", "check", event.src_path],
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                print("⚠️ [WATCHDOG] Найдены помарки/ошибки:")
                print(result.stdout)
            else:
                print("✅ Код чист.")


def detect_faulty_file(stderr_output: str) -> str:
    matches = re.findall(r'File "([^"]+\.py)"', stderr_output)
    if matches:
        project_dir = os.path.abspath(os.path.dirname(__file__))
        for file_path in reversed(matches):
            abs_path = os.path.abspath(file_path)
            if abs_path.startswith(project_dir) and "site-packages" not in abs_path:
                return os.path.basename(abs_path)
    return ENTRY_SCRIPT


def check_syntax(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError as e:
        logger.error(f"❌ Синтаксическая ошибка в сгенерированном коде: {e}")
        return False


def handle_missing_libraries(stderr_output: str) -> bool:
    match = re.search(r"ModuleNotFoundError: No module named '([^']+)'", stderr_output)
    if match:
        missing_module = match.group(1)
        logger.warning(f"📦 Обнаружена недостающая библиотека: {missing_module}")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", missing_module]
            )
            logger.info(f"✅ Успешно установлена библиотека `{missing_module}`.")
            return True
        except subprocess.CalledProcessError:
            logger.error(f"❌ Не удалось установить библиотеку `{missing_module}`.")
    return False


def send_telegram_notification(text: str):
    if notifier_bot and MY_CHAT_ID:
        try:
            notifier_bot.send_message(MY_CHAT_ID, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление Watchdog: {e}")


def model_diagnose(stderr_output: str, faulty_filename: str, source_code: str) -> str:
    logger.info(f"🧠 YandexGPT: Анализируем ошибку в файле `{faulty_filename}`...")

    prompt = f"""
Ты — Senior Python QA Engineer. Проанализируй логи ошибки и исходный файл.

Файл с ошибкой: {faulty_filename}

ОШИБКА:
{stderr_output}

ИСХОДНЫЙ КОД `{faulty_filename}`:
{source_code}

Верни ТОЛЬКО валидный JSON следующего формата:
{{
  "error_type": "...",
  "root_cause": "...",
  "file_location": "...",
  "suggested_fix_summary": "..."
}}
Не добавляй никакого текста кроме JSON.
"""

    messages = [
        {"role": "system", "text": "Ты — Senior Python QA Engineer."},
        {"role": "user", "text": prompt},
    ]

    try:
        answer = ask_yandex(messages)
        return answer or "Не удалось получить диагноз."
    except Exception as e:
        logger.error(f"Ошибка диагностики: {e}")
        return "Не удалось получить диагноз."


def model_code_fix(
    diagnosis: str, faulty_filename: str, source_code: str, error_log: str
) -> str:
    logger.info(f"🤖 YandexGPT: Исправляем файл `{faulty_filename}`...")

    prompt = f"""
Ты — Lead Python Developer. Исправь ошибку в файле {faulty_filename} на основе трассировки и диагноза.

ОШИБКА:
{error_log}

ДИАГНОЗ:
{diagnosis}

ИСХОДНЫЙ КОД `{faulty_filename}`:
{source_code}

Верни ТОЛЬКО исправленный Python-код. Без комментариев, без пояснений, без ```python блоков.
"""

    messages = [
        {"role": "system", "text": "Ты — Lead Python Developer."},
        {"role": "user", "text": prompt},
    ]

    try:
        fixed_code = ask_yandex(messages)
        if not fixed_code:
            return source_code

        fixed_code = fixed_code.strip()
        if fixed_code.startswith("```"):
            fixed_code = re.sub(r"^```(?:python)?\n?", "", fixed_code)
            fixed_code = re.sub(r"\n?```$", "", fixed_code)

        return fixed_code
    except Exception as e:
        logger.error(f"❌ Ошибка вызова YandexGPT для исправления: {e}")
        return source_code


def run_watchdog():
    path_to_watch = "."
    event_handler = QualityCheckHandler()
    observer = Observer()
    observer.schedule(event_handler, path_to_watch, recursive=False)
    observer.start()
    logger.info("🔍 Мониторинг изменений в .py файлах запущен.")

    attempts = 0

    while attempts < MAX_RETRY_ATTEMPTS:
        logger.info(f"🚀 Запуск главного процесса `{ENTRY_SCRIPT}`...")

        process = subprocess.Popen(
            [sys.executable, "-u", ENTRY_SCRIPT],
            stdout=sys.stdout,
            stderr=subprocess.PIPE,
            text=True,
        )

        _, stderr_output = process.communicate()
        exit_code = process.returncode

        if exit_code == 0:
            logger.info("✅ Приложение завершило работу без ошибок.")
            break

        logger.error(f"⚠️ Скрипт упал с кодом {exit_code}!")
        attempts += 1

        if handle_missing_libraries(stderr_output):
            time.sleep(2)
            continue

        faulty_file = detect_faulty_file(stderr_output)
        logger.info(f"🎯 Эпицентр ошибки обнаружен в файле: `{faulty_file}`")

        try:
            with open(faulty_file, "r", encoding="utf-8") as f:
                source_code = f.read()
        except Exception as e:
            logger.error(f"Не удалось прочитать файл {faulty_file}: {e}")
            break

        diagnosis = model_diagnose(stderr_output, faulty_file, source_code)
        send_telegram_notification(
            f"🐕 <b>Watchdog нашёл ошибку</b> в файле <code>{faulty_file}</code>\n\n"
            f"{diagnosis}"
        )

        print("\n" + "=" * 60)
        print(f"📋 ДИАГНОЗ И ПЛАН РЕМОНТА ДЛЯ `{faulty_file}`:")
        print("=" * 60)
        print(diagnosis)
        print("=" * 60 + "\n")

        confirm_fix = (
            input(f"👉 Исправить `{faulty_file}` через OpenRouter? (y/n): ")
            .strip()
            .lower()
        )
        if confirm_fix not in ["y", "yes", "д", "да"]:
            logger.info("⛔ Авто-исправление отменено.")
            break

        fixed_code = model_code_fix(diagnosis, faulty_file, source_code, stderr_output)

        if check_syntax(fixed_code):
            with open(f"{faulty_file}.bak", "w", encoding="utf-8") as b:
                b.write(source_code)

            with open(faulty_file, "w", encoding="utf-8") as f:
                f.write(fixed_code)

            logger.info(
                f"✨ Файл `{faulty_file}` успешно обновлен! Перезапуск через 3 сек..."
            )

            if faulty_file == os.path.basename(__file__):
                logger.info("🔄 Вочдог обновил свой код. Перезапуск...")
                os.execv(sys.executable, [sys.executable] + sys.argv)

            time.sleep(3)
        else:
            logger.error("❌ Сгенерирован невалидный код. Отмена записи.")

    observer.stop()
    observer.join()
    logger.info("👋 Мониторинг качества кода остановлен.")


if __name__ == "__main__":
    try:
        run_watchdog()
    except Exception as err:
        logger.critical(f"💥 Критическая ошибка в самом Watchdog: {err}")
