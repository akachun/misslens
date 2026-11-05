"""임베딩: sentence-transformers로 bge-m3를 프로세스당 한 번 불러온다(ARCHITECTURE §8, D17).

모델 이름은 Bedrock 모델과 나란히 config/models.yaml(`embedding:`)에서 가져온다.
테스트는 `name`, `dim`, `encode(texts) -> np.ndarray`(L2 정규화된 float32 행)를 갖춘 아무 객체나 쓴다.
"""

from __future__ import annotations

import threading
from typing import Protocol

import numpy as np
import yaml

from misslens.bedrock import DEFAULT_CONFIG_PATH


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray: ...


def embedding_config() -> dict:
    """config/models.yaml의 `embedding:`. 모델 이름은 여기에만 둔다(Bedrock ID와 같은 규칙)."""
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    if not raw.get("embedding", {}).get("model"):
        raise ValueError(f"{DEFAULT_CONFIG_PATH}: missing embedding.model")
    return raw["embedding"]


class SentenceTransformerEmbedder:
    def __init__(self, model: str, dim: int, batch_size: int = 16):
        from sentence_transformers import SentenceTransformer  # 무거운 import라 실제로 쓸 때만 한다

        self.name = model
        self.batch_size = batch_size
        try:  # 캐시된 사본 먼저: 시작할 때 Hub 왕복이 없고 오프라인에서도 동작
            self._model = SentenceTransformer(model, local_files_only=True)
        except OSError:
            self._model = SentenceTransformer(model)  # 첫 실행: 내려받기
        get_dim = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension
        self.dim = int(get_dim() or dim)

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                  convert_to_numpy=True, show_progress_bar=len(texts) > 64)
        return np.asarray(vecs, dtype=np.float32)


_lock = threading.Lock()
_default: SentenceTransformerEmbedder | None = None


def get_embedder() -> SentenceTransformerEmbedder:
    """프로세스 전체에서 쓰는 embedder(첫 호출에 bge-m3를 불러오느라 수십 초 걸리고, 이후 캐시된다)."""
    global _default
    with _lock:
        if _default is None:
            cfg = embedding_config()
            _default = SentenceTransformerEmbedder(cfg["model"], int(cfg.get("dim", 1024)),
                                                   int(cfg.get("batch_size", 16)))
        return _default
