import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import requests
from dotenv import load_dotenv
from qdrant_client import QdrantClient, models
from qdrant_client.models import Distance, PointStruct, VectorParams

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MemoryManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self, collection_name: str = "agent_memory", db_path: str = "./qdrant_db"
    ):
        if self._initialized:
            return

        self.collection_name = collection_name
        load_dotenv()

        self.api_key = (os.getenv("YANDEX_API_KEY") or "").strip()
        self.folder_id = (os.getenv("YANDEX_FOLDER_ID") or "").strip()

        if not self.api_key or not self.folder_id:
            logger.error("❌ YANDEX_API_KEY или YANDEX_FOLDER_ID не найдены в .env")
            sys.exit(1)

        try:
            self.qdrant = QdrantClient(path=db_path)
            logger.info("✅ Подключение к локальной базе Qdrant успешно установлено.")
        except Exception:
            logger.error(
                f"❌ Ошибка блокировки Qdrant ({db_path}): База занята другим процессом. "
                f"Убедитесь, что в Диспетчере задач не висят старые процессы python.exe."
            )
            raise

        self._ensure_collection_exists()
        self._initialized = True

    def _ensure_collection_exists(self):
        try:
            if not self.qdrant.collection_exists(self.collection_name):
                logger.info(
                    f"🛠 Создаем векторную коллекцию '{self.collection_name}'..."
                )
                self.qdrant.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=256,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("✅ Коллекция успешно создана!")
        except Exception as e:
            logger.error(f"❌ Ошибка при инициализации коллекции Qdrant: {e}")

    @lru_cache(maxsize=128)
    def _cached_embedding(self, text: str, embedding_type: str = "doc") -> tuple:
        result = self._get_embedding(text, embedding_type)
        return tuple(result)

    def _get_embedding(self, text: str, embedding_type: str = "doc") -> list[float]:
        try:
            model_name = (
                "text-search-query/latest"
                if embedding_type == "query"
                else "text-search-doc/latest"
            )

            url = "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding"
            headers = {
                "Authorization": f"Api-Key {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "modelUri": f"emb://{self.folder_id}/{model_name}",
                "text": text,
            }
            response = requests.post(url, headers=headers, json=payload, timeout=20)
            if response.status_code != 200:
                logger.error("❌ Ошибка эмбеддингов Yandex: %.300s", response.text)
                return []
            return response.json().get("embedding", [])
        except Exception as e:
            logger.error(f"❌ Ошибка при получении вектора: {e}")
            return []

    def get_embedding(self, text: str) -> list[float]:
        """Возвращает вектор для текста. Используется в семантической дедупликации."""
        return list(self._cached_embedding(text, "doc"))

    def save_memory(
        self, text: str, role: str = "user", metadata: dict[str, Any] | None = None
    ) -> str:
        if not text or not text.strip():
            return ""

        payload = {
            "text": text,
            "role": role,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            payload.update(metadata)

        vector = list(self._cached_embedding(text, "doc"))
        if len(vector) != 256:
            logger.error(
                "❌ Неверная размерность вектора: %d, ожидается 256", len(vector)
            )
            return ""

        point_id = str(uuid.uuid4())
        try:
            with self._lock:
                self.qdrant.upsert(
                    collection_name=self.collection_name,
                    points=[PointStruct(id=point_id, vector=vector, payload=payload)],
                )
            logger.info(f"🧠 Запомнил [{role}]: {text[:40]}...")
            return point_id
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения в память: {e}")
            return ""

    def semantic_search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        logger.info(f"🔍 Ищем в памяти воспоминания для: '{query}'...")

        query_vector = list(self._cached_embedding(query, "query"))
        if len(query_vector) != 256:
            logger.error("❌ Неверная размерность вектора запроса")
            return []

        try:
            with self._lock:
                search_response = self.qdrant.query_points(
                    collection_name=self.collection_name,
                    query=query_vector,
                    limit=limit,
                )
            results = []
            for hit in search_response.points:
                results.append(
                    {
                        "score": round(hit.score, 3),
                        "text": hit.payload.get("text", ""),
                        "role": hit.payload.get("role", "unknown"),
                    }
                )
            return results
        except Exception as e:
            logger.error(f"❌ Ошибка векторного поиска: {e}")
            return []

    def get_rag_context(
        self, query: str, limit: int = 5, min_score: float = 0.60
    ) -> str:
        memories = self.semantic_search(query, limit=limit)
        filtered_memories = [m for m in memories if m["score"] >= min_score]

        if not filtered_memories:
            return ""

        context_lines = ["\n--- ИЗВЛЕЧЕННЫЙ ИЗ ПАМЯТИ ПРОШЛЫЙ ОПЫТ ---"]
        for mem in filtered_memories:
            context_lines.append(f"• [{mem['role'].upper()}]: {mem['text']}")
        context_lines.append("-------------------------------------------\n")
        return "\n".join(context_lines)

    def get_recent_art_prompts(self, limit: int = 5) -> list[str]:
        try:
            records, _ = self.qdrant.scroll(
                collection_name=self.collection_name,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source",
                            match=models.MatchValue(value="yandex_art_prompt"),
                        )
                    ]
                ),
                limit=limit,
                with_payload=True,
            )
            records.sort(key=lambda r: r.payload.get("timestamp", ""), reverse=True)
            return [r.payload.get("text", "") for r in records]
        except Exception as e:
            logger.error(f"❌ Ошибка получения художественных промптов: {e}")
            return []

    def summarize_memory(self, texts: list[str]) -> str:
        """Просит YandexGPT сжать список текстов в 2-3 предложения."""
        if not texts:
            return ""

        prompt = (
            "Сожми следующие сообщения в краткое резюме (2-3 предложения), "
            "сохранив самую суть и важные детали:\n\n" + "\n".join(texts)
        )

        messages = [
            {
                "role": "system",
                "text": "Ты — анализатор памяти. Будь кратким, но точным.",
            },
            {"role": "user", "text": prompt},
        ]

        try:
            from yandex_service import ask_yandex

            return ask_yandex(messages) or ""
        except Exception as e:
            logger.error(f"❌ Ошибка суммаризации: {e}")
            return " ".join(texts)[-500:]

    def save_core_memory(
        self, text: str, metadata: dict[str, Any] | None = None
    ) -> str:
        if not text or not text.strip():
            return ""
        meta = {"source": "core_identity", "role": "system"}
        if metadata:
            meta.update(metadata)
        return self.save_memory(text=text, role="system", metadata=meta)

    def clear_core_memory(self) -> None:
        try:
            with self._lock:
                self.qdrant.delete(
                    collection_name=self.collection_name,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
                            must=[
                                models.FieldCondition(
                                    key="source",
                                    match=models.MatchValue(value="core_identity"),
                                )
                            ]
                        )
                    ),
                )
            logger.info("🧹 Core memory успешно очищена.")
        except Exception as e:
            logger.error(f"❌ Ошибка при очистке core memory: {e}")
