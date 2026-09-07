"""Meaning-based lookup over approved Learning, as a derived index.

Candidate retrieval scores a query against a stored question with
``0.65*jaccard + 0.35*SequenceMatcher + 0.18*concept_overlap``. Korean is
agglutinative and the concept table has eight entries, so two ways of asking
the same thing routinely share no tokens at all: measured on inquiry 325584049,
"폐가전 수거해주시나요" against "기존 폐가전도 무료로 수거해 주시나요?" scores
0.21 -- below the 0.24 floor -- and the customer's own typo ("페가전") pushes it
to 0.15. Sixty-three approved answers about collection were in the store and
none of them was a candidate.

Adding words to the concept table would be the same mechanism again, one
phrase at a time. This is the other kind of fix: the question and the stored
question are compared as meanings, by embedding, so wording, spacing and typos
stop deciding recall.

Two things this deliberately is not:

* It is not a replacement for the lexical scorer. An exact model code or a
  quoted policy phrase is precisely what lexical matching is good at, and the
  two are unioned rather than swapped.
* It is not the selection stage. Recall is what this buys; whether a candidate
  actually answers the question is decided afterwards, by compatibility, the
  evidence selector and the verifier.

The index is derived data. ``learning_examples`` remains the source of truth,
the index is rebuilt from it, and losing the file costs an embedding pass and
nothing else.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
from typing import Any, Iterable, Mapping, Sequence

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
EMBEDDING_ENDPOINT = "https://api.openai.com/v1/embeddings"
API_KEY_ENV = "QNA_GPT_API_KEY"

# Where the derived index lives. Beside the database it is derived from, and
# never inside it: a rebuild must not touch Learning rows.
DEFAULT_INDEX_PATH = pathlib.Path("data") / "learning_semantic_index.json"

# One request per this many rows. The endpoint accepts batches and the whole
# active corpus is ~1,000 rows, so a rebuild is a handful of calls.
BATCH_SIZE = 128

# 프로세스 단위 캐시. 파일 경로와 mtime 이 키다.
_LOADED: dict[tuple, Any] = {}


def _text_for(row: Mapping[str, Any]) -> str:
    """What of a Learning row carries its meaning.

    The question says what was asked and the answer says what was settled; a
    query may resemble either, so both are embedded together. Truncated
    because an embedding of a very long support reply drifts toward its
    boilerplate greeting and away from the fact it carries.
    """

    question = " ".join(str(row.get("question") or "").split())
    answer = " ".join(str(row.get("answer") or "").split())
    return ("%s\n%s" % (question[:400], answer[:600])).strip()


def _normalise(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Both sides are stored normalised, so this is a dot product."""

    return sum(a * b for a, b in zip(left, right))


class EmbeddingClient:
    """The one network call this module makes."""

    def __init__(self, *, model: str = EMBEDDING_MODEL) -> None:
        self.model = model
        self.calls = 0
        self.tokens = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import requests

        key = os.environ.get(API_KEY_ENV) or ""
        if not key:
            raise RuntimeError("%s is not set" % API_KEY_ENV)
        response = requests.post(
            EMBEDDING_ENDPOINT,
            headers={"Authorization": "Bearer %s" % key,
                     "Content-Type": "application/json"},
            json={"model": self.model, "input": list(texts)},
            timeout=(5, 120),
        )
        response.raise_for_status()
        payload = response.json()
        self.calls += 1
        self.tokens += int((payload.get("usage") or {}).get("total_tokens") or 0)
        ordered = sorted(payload["data"], key=lambda item: item["index"])
        return [_normalise(item["embedding"]) for item in ordered]


class LearningSemanticIndex:
    """Cosine lookup over the approved corpus, loaded from the derived file."""

    def __init__(self, vectors: Mapping[int, Sequence[float]] | None = None,
                 *, model: str = EMBEDDING_MODEL) -> None:
        self.vectors: dict[int, list[float]] = {
            int(key): list(value) for key, value in (vectors or {}).items()
        }
        self.model = model

    # ------------------------------------------------------------- 저장/적재
    @classmethod
    def load_cached(cls, path: pathlib.Path | str = DEFAULT_INDEX_PATH
                    ) -> "LearningSemanticIndex":
        """One load per process, keyed by path and mtime.

        The index is tens of megabytes and a compound inquiry builds several
        contexts; re-reading it per sub-question turned retrieval into file
        I/O. The mtime is part of the key so a rebuilt index is picked up
        without restarting.
        """

        file = pathlib.Path(path)
        try:
            stamp = file.stat().st_mtime_ns
        except OSError:
            stamp = 0
        key = (str(file), stamp)
        cached = _LOADED.get(key)
        if cached is None:
            cached = cls.load(file)
            _LOADED.clear()
            _LOADED[key] = cached
        return cached

    @classmethod
    def load(cls, path: pathlib.Path | str = DEFAULT_INDEX_PATH
             ) -> "LearningSemanticIndex":
        """An absent or unreadable index is an empty one, never an error.

        Retrieval falls back to the lexical scorer alone in that case, which is
        exactly the behaviour that shipped before this module existed.
        """

        file = pathlib.Path(path)
        if not file.exists():
            return cls({})
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
            vectors = {
                int(key): value for key, value in (payload.get("vectors") or {}).items()
            }
            return cls(vectors, model=str(payload.get("model") or EMBEDDING_MODEL))
        except (ValueError, OSError, TypeError):
            return cls({})

    def save(self, path: pathlib.Path | str = DEFAULT_INDEX_PATH) -> pathlib.Path:
        file = pathlib.Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({
            "model": self.model,
            "dimensions": EMBEDDING_DIMENSIONS,
            "count": len(self.vectors),
            "vectors": {str(key): value for key, value in self.vectors.items()},
        }, ensure_ascii=False), encoding="utf-8")
        return file

    # --------------------------------------------------------------- 조회
    @property
    def available(self) -> bool:
        return bool(self.vectors)

    def similar(self, query_vector: Sequence[float], *, limit: int = 30,
                minimum: float = 0.0) -> list[tuple[int, float]]:
        scored = [
            (identifier, cosine(query_vector, vector))
            for identifier, vector in self.vectors.items()
        ]
        scored = [item for item in scored if item[1] >= minimum]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]

    # --------------------------------------------------------------- 구축
    @classmethod
    def build(cls, rows: Iterable[Mapping[str, Any]], *,
              client: EmbeddingClient | None = None,
              batch_size: int = BATCH_SIZE,
              progress: Any = None) -> "LearningSemanticIndex":
        """Embed the rows given. Callers decide which rows those are.

        Passing the row set in rather than querying here keeps this module free
        of the repository and makes a partial rebuild -- only the rows that
        changed -- a matter of passing fewer rows.
        """

        client = client or EmbeddingClient()
        items = [
            (int(row["id"]), _text_for(row))
            for row in rows
            if row.get("id") is not None and _text_for(row)
        ]
        vectors: dict[int, list[float]] = {}
        for start in range(0, len(items), batch_size):
            chunk = items[start:start + batch_size]
            embedded = client.embed([text for _identifier, text in chunk])
            for (identifier, _text), vector in zip(chunk, embedded):
                vectors[identifier] = vector
            if progress is not None:
                progress(min(start + batch_size, len(items)), len(items))
        return cls(vectors, model=client.model)

    def merge(self, other: "LearningSemanticIndex") -> "LearningSemanticIndex":
        """Fold a partial rebuild into this index."""

        merged = dict(self.vectors)
        merged.update(other.vectors)
        return LearningSemanticIndex(merged, model=self.model)

    def drop(self, identifiers: Iterable[int]) -> "LearningSemanticIndex":
        """Remove rows that are no longer active. The rows themselves stay."""

        gone = {int(item) for item in identifiers}
        return LearningSemanticIndex(
            {key: value for key, value in self.vectors.items() if key not in gone},
            model=self.model,
        )
