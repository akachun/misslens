"""Embeddings: bge-m3 via sentence-transformers, loaded once per process (ARCHITECTURE §8, D17).

The model name comes from config/models.yaml (`embedding:`), next to the Bedrock models.
Tests use any object with `name`, `dim` and `encode(texts) -> np.ndarray` (L2-normalized float32 rows).
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
    """`embedding:` in config/models.yaml, the only place the model name lives (same rule as Bedrock IDs)."""
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    if not raw.get("embedding", {}).get("model"):
        raise ValueError(f"{DEFAULT_CONFIG_PATH}: missing embedding.model")
    return raw["embedding"]


class SentenceTransformerEmbedder:
    def __init__(self, model: str, dim: int, batch_size: int = 16):
        from sentence_transformers import SentenceTransformer  # heavy import, only when really used

        self.name = model
        self.batch_size = batch_size
        try:  # cached copy first: no Hub round trips at startup, works offline
            self._model = SentenceTransformer(model, local_files_only=True)
        except OSError:
            self._model = SentenceTransformer(model)  # first run: download
        get_dim = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension
        self.dim = int(get_dim() or dim)

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                  convert_to_numpy=True, show_progress_bar=len(texts) > 64)
        return np.asarray(vecs, dtype=np.float32)


_lock = threading.Lock()
_default: SentenceTransformerEmbedder | None = None


def get_embedder() -> SentenceTransformerEmbedder:
    """The process-wide embedder (first call loads bge-m3: tens of seconds, then cached)."""
    global _default
    with _lock:
        if _default is None:
            cfg = embedding_config()
            _default = SentenceTransformerEmbedder(cfg["model"], int(cfg.get("dim", 1024)),
                                                   int(cfg.get("batch_size", 16)))
        return _default
