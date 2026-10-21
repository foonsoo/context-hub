"""Optional local embeddings for semantic search (``pip install 'context-hub[embed]'``).

Lexical search stays the default and the fallback. Semantic search adds what
words alone miss: paraphrases ("엑셀로 올리는 방향" ~ "Excel 업로드 중심") and
cross-language questions. The model runs locally (ONNX via fastembed), so no
text leaves the machine.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol, Sequence

from .hub import Hub

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
TEXT_LIMIT = 1000
# Cosine similarity is mapped to 0..1 relevance above this floor; unrelated text sits near 0.2-0.38.
COSINE_FLOOR = 0.2
# Gates for injecting knowledge nobody asked for (hooks). Calibrated on the real hub and on a
# small hub of short notes: short Korean sentences sit at 0.4-0.56 cosine even when unrelated,
# so semantics alone must be strong AND stand out from the rest of the corpus.
STRONG_SEMANTIC = 0.6
STRONG_MARGIN = 0.3  # cosine above the mean of the other pages
LEXICAL_WITH_SUPPORT = (0.5, 0.35)  # 0.25 let "도커 이미지" pull in an unrelated "앱 이미지" note


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def page_text(title: str, body: str) -> str:
    return f"{title}\n{body}"[:TEXT_LIMIT]


def content_hash(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\n{text}".encode()).hexdigest()[:16]


def cache_dir() -> Path:
    import os

    return Path(os.environ.get("CTX_MODEL_CACHE") or Path.home() / ".cache" / "context-hub" / "models")


class FastEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL):
        from fastembed import TextEmbedding  # ImportError means "not installed"

        self.model = model
        self._engine = TextEmbedding(model, cache_dir=str(cache_dir()))

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for vector in self._engine.embed(list(texts)):
            norm = sum(float(x) * float(x) for x in vector) ** 0.5 or 1.0
            vectors.append([float(x) / norm for x in vector])
        return vectors


def installed() -> bool:
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


def enabled(hub: Hub) -> bool:
    return str(hub.setting("search", "embeddings", "CTX_EMBEDDINGS", "off")).casefold() in ("on", "1", "true")


_CACHE: dict[str, Embedder] = {}


def load(hub: Hub) -> Embedder | None:
    """The configured embedder, or None when disabled/unavailable (lexical search only)."""
    if not enabled(hub):
        return None
    model = str(hub.setting("search", "embedding_model", "CTX_EMBEDDING_MODEL", DEFAULT_MODEL))
    if model not in _CACHE:
        try:
            _CACHE[model] = FastEmbedder(model)
        except Exception as exc:  # missing package or model files: fall back quietly
            hub.log(f"embeddings unavailable: {exc!r}")
            return None
    return _CACHE[model]


def relevance(cosine: float) -> float:
    return max(0.0, (cosine - COSINE_FLOOR) / (1 - COSINE_FLOOR))
