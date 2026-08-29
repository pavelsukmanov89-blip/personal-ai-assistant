import os

import certifi
from dotenv import load_dotenv
from telebot import apihelper

load_dotenv()

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
MY_CHAT_ID = (os.getenv("MY_CHAT_ID") or "").strip()
YANDEX_ART_API_KEY = (os.getenv("YANDEX_ART_API_KEY") or "").strip()
YANDEX_FOLDER_ID = (os.getenv("YANDEX_FOLDER_ID") or "").strip()
ADMIN_ID = (os.getenv("ADMIN_ID") or "0").strip()

if not TELEGRAM_BOT_TOKEN or not MY_CHAT_ID:
    raise ValueError("TELEGRAM_BOT_TOKEN или MY_CHAT_ID не заданы в .env")

if not YANDEX_ART_API_KEY or not YANDEX_FOLDER_ID:
    raise ValueError("YANDEX_ART_API_KEY или YANDEX_FOLDER_ID не заданы в .env")

# Лечим ошибку SSL на Windows
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ.pop("CURL_CA_BUNDLE", None)

apihelper.CONNECT_TIMEOUT = 15
apihelper.READ_TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/114.0.0.0 Safari/537.36"
    )
}

# -----------------------------------------------------------
# RSS по темам (качество важнее количества)
# -----------------------------------------------------------
RSS_GROUPS = {
    "python": [
        "https://planetpython.org/rss20.xml",
        "https://blog.python.org/feeds/posts/default",
        "https://realpython.com/atom.xml",
    ],
    "ai": [
        "https://hnrss.org/frontpage",
        "https://techcrunch.com/category/artificial-intelligence/feed/",
    ],
    "hardware": [
        "https://www.tomshardware.com/feeds/all",
    ],
    "astronomy": [
        "https://www.nasa.gov/news-release/feed/",
        "https://www.space.com/feeds/all",
    ],
    "analytics": [
        "https://www.infoq.com/feed",
        "https://martinfowler.com/feed.atom",
    ],
    "science": [
        "https://sciencedaily.com/rss/all.xml",
        "https://www.quantamagazine.org/feed/",
        "https://phys.org/rss-feed/",
    ],
    "world": [
        "https://www.interfax.ru/rss.asp",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
    ],
    "finance": [
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "https://www.investing.com/rss/news.rss",
    ],
    "demis_hassabis": [
        "https://news.google.com/rss/search?q=Demis+Hassabis&hl=en-US&gl=US&ceid=US:en",
    ],
}

# Плоский список для совместимости с get_raw_news
TRUSTED_RSS_FEEDS = [url for urls in RSS_GROUPS.values() for url in urls]
