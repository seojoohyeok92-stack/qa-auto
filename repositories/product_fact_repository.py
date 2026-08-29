"""Read-only access to the separate Product Knowledge database.

``product_facts.db`` is a distinct knowledge source built by another project.
It is never written, never migrated, and never merged into
``oje_automation.db``: this repository opens it with ``mode=ro`` *and*
``PRAGMA query_only=ON`` so that a stray write fails loudly instead of
corrupting a shared source of truth.

The repository is deliberately thin. It returns rows plus the status columns
that decide trust; deciding whether a fact may be quoted to a customer belongs
to :mod:`services.product_knowledge_service`, so that judgement lives in one
place and cannot be bypassed by a caller that queries the tables directly.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRODUCT_FACTS_DB_PATH_ENV = "OJE_PRODUCT_FACTS_DB_PATH"
DEFAULT_PRODUCT_FACTS_DB_PATH = Path("data") / "product_facts.db"
BUSY_TIMEOUT_MS = 5_000

# Canonical facts and their provenance are versioned in place: a re-run marks
# the previous row SUPERSEDED rather than deleting it. Only ACTIVE rows
# describe the product as it is understood now.
ACTIVE_LIFECYCLE = "ACTIVE"


def get_product_facts_path(
    path: str | os.PathLike[str] | None = None,
) -> Path:
    configured = path if path is not None else os.getenv(
        PRODUCT_FACTS_DB_PATH_ENV
    )
    return Path(configured) if configured else DEFAULT_PRODUCT_FACTS_DB_PATH


class ProductFactsUnavailableError(RuntimeError):
    """The Product Knowledge database is absent or unreadable.

    Never fatal to answering: the pipeline simply proceeds with no product
    facts, which leaves every existing gate exactly as it was.
    """


class ProductFactRepository:
    """READ-ONLY reader for ``product_facts.db``."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = get_product_facts_path(path)

    def available(self) -> bool:
        return self.path.is_file()

    def identity(self, *, digest: bool = False) -> dict[str, Any]:
        """Which Product Facts artifact this process is actually reading.

        The knowledge base is a file that gets replaced, and two copies with
        the same name can be months apart: the development machine ran for
        weeks on a snapshot taken before a normalization pass, and nothing in
        the running system could say so. Path, size and mtime answer that
        cheaply enough to record on every diagnostic.

        ``digest`` reads the whole file to produce a SHA-256, which is the only
        way to prove two copies are the same artifact. It is off by default
        because the file is ~57 MB: ask for it in a diagnostic, never on the
        answering path.
        """

        info: dict[str, Any] = {
            "path": str(self.path),
            "available": self.available(),
            "size_bytes": None,
            "modified_at": None,
            "sha256": None,
        }
        if not info["available"]:
            return info
        stat = self.path.stat()
        info["size_bytes"] = stat.st_size
        info["modified_at"] = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
        if digest:
            hasher = hashlib.sha256()
            with self.path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            info["sha256"] = hasher.hexdigest()
        return info

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise ProductFactsUnavailableError(
                f"Product Facts DB not found: {self.path}"
            )
        uri = "file:{}?mode=ro".format(str(self.path).replace("\\", "/"))
        connection = sqlite3.connect(
            uri, uri=True, isolation_level=None,
            timeout=BUSY_TIMEOUT_MS / 1_000,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS:d}")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise ProductFactsUnavailableError(
                    "READ_ONLY_GUARD_FAILED: PRAGMA query_only is not ON"
                )
            yield connection
        finally:
            connection.close()

    # ------------------------------------------------------------------
    def listing_for_product(self, product_id: object) -> dict[str, Any] | None:
        """The listing row this Naver ``product_id`` maps to, if any."""

        key = str(product_id or "").strip()
        if not key:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT listing_id, product_id, input_listing_name, "
                "pilot_category, collection_status "
                "FROM listings WHERE product_id = ? LIMIT 1",
                (key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def facts_for_product(
        self,
        product_id: object,
        fields: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Canonical facts attached to one product, with trust columns.

        Only the *selected* canonical value is joined: a fact's other
        candidate values are what the resolution status is about, and quoting
        one of them would be quoting a value the canonicaliser rejected.
        Rows are returned whatever their status -- filtering belongs to the
        service, which also has to explain why something was excluded.
        """

        key = str(product_id or "").strip()
        if not key:
            return []
        wanted = [str(item).strip() for item in (fields or ()) if str(item).strip()]
        clause = ""
        params: list[Any] = [key]
        if wanted:
            clause = " AND cf.field IN ({})".format(
                ",".join("?" for _ in wanted)
            )
            params.extend(wanted)
        sql = f"""
            SELECT
                cf.canonical_fact_id, cf.field, cf.scope, cf.scope_key,
                cf.volatility, cf.verification_status, cf.resolution_status,
                cf.lifecycle_status, cf.selected_value_id, cf.identity_source,
                cf.last_verified_at, cf.valid_from, cf.valid_until,
                cfv.value_id, cfv.normalized_value_json, cfv.raw_value_json,
                cfv.relationship_status,
                cfl.listing_id, cfl.product_id, cfl.model_code
            FROM canonical_fact_listings cfl
            JOIN canonical_facts cf
              ON cf.canonical_fact_id = cfl.canonical_fact_id
            LEFT JOIN canonical_fact_values cfv
              ON cfv.value_id = cf.selected_value_id
            WHERE cfl.product_id = ?{clause}
            ORDER BY cf.field
        """
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def provenance_for_values(
        self,
        pairs: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """ACTIVE provenance for each (canonical_fact_id, value_id) pair.

        Provenance is fetched for the *selected* value only. A source that
        backed a different candidate value does not support the value the
        answer would quote, and a SUPERSEDED source describes an earlier
        collection run.
        """

        keys = [
            (str(fact_id), str(value_id))
            for fact_id, value_id in pairs
            if str(fact_id or "").strip() and str(value_id or "").strip()
        ]
        if not keys:
            return {}
        result: dict[tuple[str, str], list[dict[str, Any]]] = {}
        placeholders = ",".join("?" for _ in keys)
        sql = f"""
            SELECT canonical_provenance_id, canonical_fact_id, value_id,
                   source_type, source_url, source_section, source_locator,
                   source_text, source_status, lifecycle_status, confidence,
                   analyzer, analyzer_version, collected_at
            FROM canonical_fact_provenance
            WHERE lifecycle_status = ?
              AND canonical_fact_id IN ({placeholders})
        """
        with self.connection() as connection:
            rows = connection.execute(
                sql, [ACTIVE_LIFECYCLE, *[item[0] for item in keys]]
            ).fetchall()
        wanted = set(keys)
        for row in rows:
            key = (str(row["canonical_fact_id"]), str(row["value_id"]))
            if key not in wanted:
                continue
            result.setdefault(key, []).append(dict(row))
        return result
