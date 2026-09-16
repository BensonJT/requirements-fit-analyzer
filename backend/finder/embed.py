"""Sentence encoder for requirement coverage (sprint plan §15.3; the §9 loader without the centroid).

`load_encoder()` returns an object with `name`, `dim` and `encode(texts, query=False) -> np.ndarray` (float32,
L2-normalized, one row per text). bge models take an instruction prefix on the query side only: requirements
are queries, evidence units are passages. sentence-transformers is tried first, then fastembed; both are
imported inside `load_encoder`, so importing this module costs nothing.
"""
import json
import os
from typing import Optional

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
INSTALL_HINT = ("no embedding backend: `.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch "
                "&& .venv/bin/pip install sentence-transformers` (or `pip install fastembed`)")


class EncoderUnavailable(RuntimeError):
    """Raised when no embedding library is installed."""


class _STEncoder:
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer
        self.name, self.backend = model_name, "st"
        self._model = SentenceTransformer(model_name, device="cpu")
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: list, query: bool = False, batch_size: int = 64):
        import numpy as np
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        prefix = QUERY_INSTRUCTION if query and "bge" in self.name.lower() else ""
        vecs = self._model.encode([prefix + t for t in texts], batch_size=batch_size, normalize_embeddings=True,
                                  show_progress_bar=False, convert_to_numpy=True)
        return vecs.astype(np.float32)


class _FastEmbedEncoder:
    def __init__(self, model_name: str):
        from fastembed import TextEmbedding
        self.name, self.backend, self.dim = model_name, "fastembed", EMBED_DIM
        self._model = TextEmbedding(model_name)

    def encode(self, texts: list, query: bool = False, batch_size: int = 64):
        import numpy as np
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        prefix = QUERY_INSTRUCTION if query and "bge" in self.name.lower() else ""
        vecs = np.asarray(list(self._model.embed([prefix + t for t in texts], batch_size=batch_size)), dtype=np.float32)
        return normalize(vecs)


class Reranker:
    """A cross-encoder that reads a (requirement, evidence) pair together and returns a 0-1 relevance (sigmoid of
    the logit). Experiment S4 (docs/COVERAGE_EXPERIMENTS.md). Scores are cached per exact pair."""

    def __init__(self, model_name: str):
        from sentence_transformers import CrossEncoder
        self.name = model_name
        self._model = CrossEncoder(model_name, device="cpu")
        self._cache = {}
        self.pairs_scored = 0

    def score(self, pairs: list):
        import numpy as np
        todo = [p for p in dict.fromkeys(pairs) if p not in self._cache]
        if todo:
            import torch
            raw = self._model.predict(todo, batch_size=64, activation_fn=torch.nn.Identity(), show_progress_bar=False)
            logits = np.asarray(raw, dtype=np.float64).reshape(len(todo), -1)[:, -1]
            self._cache.update(zip(todo, (1.0 / (1.0 + np.exp(-logits))).tolist()))
            self.pairs_scored += len(todo)
        return np.asarray([self._cache[p] for p in pairs], dtype=np.float32)


def normalize(vecs):
    import numpy as np
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return (vecs / np.where(norms == 0, 1, norms)).astype(np.float32)


def available() -> Optional[str]:
    """'st' or 'fastembed' when that library imports, else None (no model is loaded)."""
    import importlib.util
    for backend, module in (("st", "sentence_transformers"), ("fastembed", "fastembed")):
        if importlib.util.find_spec(module) is not None:
            return backend
    return None


def load_encoder(backend: Optional[str] = None, model_name: Optional[str] = None):
    """The encoder for `backend` ('st' | 'fastembed'), JOBSEARCH_EMBED_BACKEND, or the first that imports."""
    backend = backend or os.getenv("JOBSEARCH_EMBED_BACKEND") or None
    model_name = model_name or EMBED_MODEL
    order = [backend] if backend else ["st", "fastembed"]
    errors = []
    for b in order:
        try:
            return _STEncoder(model_name) if b == "st" else _FastEmbedEncoder(model_name)
        except ImportError as exc:
            errors.append(f"{b}: {exc}")
    raise EncoderUnavailable(INSTALL_HINT + " [" + "; ".join(errors) + "]")


def vectors_json(vecs) -> str:
    """A JSON array of float lists (5 decimals) for DuckDB's json_transform -> FLOAT[] -> FLOAT[dim]."""
    import numpy as np
    return json.dumps(np.round(np.asarray(vecs, dtype=np.float64), 5).tolist())


def stack(column) -> "np.ndarray":  # noqa: F821
    """fetchnumpy() of a FLOAT[n] column (object array of arrays) as one float32 matrix."""
    import numpy as np
    if len(column) == 0:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    return np.stack([np.asarray(v, dtype=np.float32) for v in column])
