from __future__ import annotations

import hashlib
import json
import re
import uuid
import os
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from config import NaverSyncSettings, StoreConfig, get_configured_stores, get_store_config
from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.learning_repository import LearningRepository
from services.learning_privacy_service import LearningPrivacyService
from services.similar_answer_service import TOKEN, normalize_learning_question

if TYPE_CHECKING:
    from services.naver_inquiry_sync_service import NaverInquirySyncService

SUPPORTED_INQUIRY_TYPES = ("PRODUCT_INQUIRY", "CUSTOMER_INQUIRY")


TIME_DEPENDENT = re.compile(
    r"(?:배송|도착|출고|설치)\s*(?:예정|가능|일|됩니다)|"
    r"재고|가격|할인|프로모션|이벤트|적립금|쿠폰|사은품|"
    r"\d{1,4}\s*(?:년|월|일|원)|\d{4}[./-]\d{1,2}[./-]\d{1,2}"
)
DEFINITE = re.compile(r"(?:반드시|무조건|확실히|100%|틀림없이|보장).{0,30}(?:배송|도착|설치|지급|가능)")
AUTO_REFERENCE_MIN_QUALITY = 0.50


def _seller_answer(value: Any) -> str:
    keys = {"selleranswer", "answercontent", "commentcontent", "replycontent", "answer"}
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[-_]", "", str(key)).lower()
            if normalized in keys and isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            found = _seller_answer(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _seller_answer(item)
            if found:
                return found
    return ""


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class HistoricalCaseService:
    """Imports and searches reference cases without touching the automation queue."""

    def __init__(
        self, database: Database, *, naver_sync: "NaverInquirySyncService | None" = None
    ) -> None:
        self.database = database
        self.repository = HistoricalCaseRepository(database)
        self.privacy = LearningPrivacyService()
        self.naver = naver_sync

    def _naver_service(self) -> "NaverInquirySyncService":
        if self.naver is None:
            # API import is the only operation that loads the Naver Sync
            # dependency. Search/prompt generation remains repository-only.
            from services.naver_inquiry_sync_service import NaverInquirySyncService
            self.naver = NaverInquirySyncService(self.database)
        return self.naver

    @staticmethod
    def _date_windows(
        from_datetime: datetime, to_datetime: datetime
    ) -> list[tuple[datetime, datetime]]:
        try:
            window_days = max(1, int(os.getenv("HISTORICAL_IMPORT_WINDOW_DAYS", "90")))
        except (TypeError, ValueError):
            window_days = 90
        windows: list[tuple[datetime, datetime]] = []
        cursor = from_datetime
        while cursor < to_datetime:
            end = min(cursor + timedelta(days=window_days), to_datetime)
            windows.append((cursor, end))
            cursor = end + timedelta(microseconds=1)
        return windows

    def preview_naver(
        self, *, store_id: str | None = None,
        inquiry_types: Sequence[str] = SUPPORTED_INQUIRY_TYPES,
        from_datetime: datetime, to_datetime: datetime,
        answered_only: bool = True, require_seller_answer: bool = True,
        max_pages: int | None = None,
    ) -> dict[str, Any]:
        """Read-only page preview that never touches inquiries or events."""
        naver = self._naver_service()
        if not naver.settings.enabled:
            raise RuntimeError("네이버 문의 조회 설정이 비활성화되어 있습니다.")
        if from_datetime.tzinfo is None or to_datetime.tzinfo is None or from_datetime >= to_datetime:
            raise ValueError("조회 기간을 확인해 주세요.")
        targets = [get_store_config(store_id)] if store_id else get_configured_stores()
        page_limit = min(max_pages or naver.settings.max_pages, naver.settings.max_pages)
        result = {
            "total_fetched": 0, "answer_count": 0, "no_answer_count": 0,
            "eligible_count": 0, "existing_count": 0, "expected_new_count": 0,
            "excluded_count": 0, "failed_count": 0, "pages_queried": 0,
            "window_count": len(self._date_windows(from_datetime, to_datetime)),
            "estimate_basis": "QUERIED_PAGES",
        }
        preview_keys: set[str] = set()
        for store in targets:
            token = naver._token(store)
            for inquiry_type in inquiry_types:
                for window_from, window_to in self._date_windows(from_datetime, to_datetime):
                    previous: tuple[str, ...] | None = None
                    for page in range(1, page_limit + 1):
                        payload = naver._fetch_page(
                            inquiry_type, token=token, page=page,
                            from_datetime=window_from, to_datetime=window_to,
                        )
                        key = "contents" if inquiry_type == "PRODUCT_INQUIRY" else "content"
                        content = payload.get(key)
                        if not isinstance(content, list):
                            raise ValueError("네이버 문의 응답 형식을 확인할 수 없습니다.")
                        result["pages_queried"] += 1
                        signature: list[str] = []
                        for raw in content:
                            if not isinstance(raw, dict):
                                result["failed_count"] += 1
                                continue
                            item = naver._normalize(inquiry_type, raw, store_code=store.code)
                            signature.append(str(item.get("external_inquiry_id") or ""))
                            result["total_fetched"] += 1
                            answer = str(item.get("seller_answer") or "").strip()
                            answered = bool(item.get("source_answered") if item.get("source_answered") is not None else item.get("answered"))
                            result["answer_count" if answer else "no_answer_count"] += 1
                            if (require_seller_answer and not answer) or (answered_only and not answered):
                                result["excluded_count"] += 1
                                continue
                            result["eligible_count"] += 1
                            case = self.prepare_case(item, source_reference="NAVER_API_PREVIEW")
                            if case["case_key"] in preview_keys:
                                result["excluded_count"] += 1
                                continue
                            preview_keys.add(case["case_key"])
                            if self.repository.get_by_case_key(case["case_key"]) is not None:
                                result["existing_count"] += 1
                            else:
                                result["expected_new_count"] += 1
                        current = tuple(signature)
                        if content and current == previous:
                            raise RuntimeError("네이버 과거 문의 페이지가 반복되어 조회를 중단했습니다.")
                        previous = current
                        if not content or len(content) < naver.settings.page_size:
                            break
        return result

    @staticmethod
    def _digest(*parts: Any) -> str:
        return hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()

    def _quality(
        self, *, question: str, answer: str, created_at: Any, repeated: int = 0
    ) -> tuple[float, str, dict[str, Any]]:
        masked_question = self.privacy.mask(question)
        masked_answer = self.privacy.mask(answer)
        sensitive = masked_question != question or masked_answer != answer
        time_dependent = bool(TIME_DEPENDENT.search(answer))
        definite = bool(DEFINITE.search(answer))
        too_short = len(answer.strip()) < 12
        created = _parse_time(created_at)
        age_days = (datetime.now(UTC) - created).days if created else None
        recent = age_days is not None and age_days <= 365
        score = 0.55
        score += 0.15 if recent else -0.10 if age_days is not None and age_days > 1095 else 0
        score += min(max(repeated, 0), 3) * 0.04
        score -= 0.25 if time_dependent else 0
        score -= 0.20 if definite else 0
        score -= 0.18 if sensitive else 0
        score -= 0.35 if too_short else 0
        if not answer.strip():
            score = 0.0
        score = round(max(0.0, min(score, 1.0)), 4)
        if not answer.strip() or score == 0:
            risk = "BLOCKED"
        elif definite or (time_dependent and sensitive):
            risk = "HIGH"
        elif time_dependent or sensitive or too_short:
            risk = "MEDIUM"
        else:
            risk = "LOW" if not recent else "NONE"
        return score, risk, {
            "recent": recent,
            "age_days": age_days,
            "repeated_answer_count": repeated,
            "time_dependent_fact": time_dependent,
            "definite_policy_expression": definite,
            "personal_information_masked": sensitive,
            "too_short": too_short,
            "facts_authority": "REFERENCE_ONLY",
        }

    def prepare_case(self, item: dict[str, Any], *, source_reference: str) -> dict[str, Any]:
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        original_question = "\n".join(part for part in (title, content) if part).strip()
        names = [item.get("customer_display"), item.get("masked_writer_id")]
        question = self.privacy.mask(original_question, customer_names=names)
        answer_raw = str(item.get("seller_answer") or "").strip()
        answer = self.privacy.mask(answer_raw, customer_names=names)
        product_name = self.privacy.mask(item.get("product_name"))
        order_reference = self.privacy.mask(item.get("order_id") or item.get("product_order_id")) or None
        normalized = normalize_learning_question(question)
        external_id = str(
            item.get("external_inquiry_id") or item.get("source_question_id") or item.get("inquiry_id") or ""
        )
        source_type = str(item.get("source_type") or item.get("source") or item.get("inquiry_type") or "UNKNOWN")
        store = str(item.get("store_code") or item.get("store_id") or "UNKNOWN")
        repeated = self.repository.answer_frequency(answer) if answer else 0
        score, risk, flags = self._quality(
            question=original_question, answer=answer_raw,
            created_at=item.get("source_created_at") or item.get("registered_at"),
            repeated=repeated,
        )
        case_key = self._digest("NAVER_HISTORY", store, source_type, external_id, normalized)
        fingerprint = self._digest(case_key, answer)
        raw = item.get("raw_payload") if isinstance(item.get("raw_payload"), dict) else {}
        safe_raw = {
            key: raw.get(key)
            for key in (
                "questionId", "inquiryNo", "inquiryId", "productId", "answered",
                "status", "createDate", "updateDate", "lastModifiedDate",
                "inquiryRegistrationDateTime", "inquiryModificationDateTime",
            )
            if raw.get(key) not in (None, "")
        }
        return {
            "source": "NAVER_HISTORY",
            "store_code": store,
            "inquiry_id": item.get("local_inquiry_id") or item.get("id"),
            "external_inquiry_id": external_id,
            "inquiry_type": source_type,
            "question": question,
            "question_normalized": normalized,
            "seller_answer": answer or None,
            "product_name": product_name or None,
            "product_id": self.privacy.mask(item.get("product_id")) or None,
            "order_reference": order_reference,
            "source_answered": bool(item.get("source_answered") if item.get("source_answered") is not None else item.get("answered")),
            "inquiry_created_at": item.get("source_created_at") or item.get("registered_at"),
            "answer_updated_at": item.get("source_updated_at") or item.get("updated_at"),
            "source_payload_reference": source_reference,
            "raw_json": safe_raw,
            "classification": str(item.get("inquiry_type") or source_type),
            "policy_risk": risk,
            "quality_score": score,
            "confidence": score,
            # Learning participation is opt-out.  Safety eligibility remains a
            # separate retrieval-time decision, even for BLOCK/HIGH cases.
            "active": True,
            "metadata_json": flags,
            "case_key": case_key,
            "fingerprint": fingerprint,
        }

    @staticmethod
    def _empty_counts() -> dict[str, int]:
        return {
            "total_fetched": 0, "inserted_count": 0, "updated_count": 0,
            "duplicate_count": 0, "no_answer_count": 0,
            "skipped_count": 0, "failed_count": 0, "last_page": 0,
        }

    def _save_items(
        self, items: Iterable[dict[str, Any]], *, counts: dict[str, int],
        require_seller_answer: bool, answered_only: bool, source_prefix: str,
    ) -> None:
        for item in items:
            counts["total_fetched"] += 1
            answer = str(item.get("seller_answer") or "").strip()
            answered = bool(item.get("source_answered") if item.get("source_answered") is not None else item.get("answered"))
            if not answer:
                counts["no_answer_count"] += 1
                if require_seller_answer:
                    counts["skipped_count"] += 1
                    continue
            if answered_only and not answered:
                counts["skipped_count"] += 1
                continue
            try:
                case = self.prepare_case(
                    item,
                    source_reference=f"{source_prefix}:{item.get('local_inquiry_id') or item.get('external_inquiry_id') or item.get('source_question_id')}",
                )
                _, outcome = self.repository.upsert(case)
                counts[f"{outcome}_count"] += 1
            except Exception:
                counts["failed_count"] += 1

    def import_local(
        self, *, store_code: str | None = None,
        inquiry_types: Sequence[str] = SUPPORTED_INQUIRY_TYPES,
        date_from: str | None = None, date_to: str | None = None,
        answered_only: bool = True, require_seller_answer: bool = True,
        batch_size: int = 200,
    ) -> dict[str, Any]:
        run = self.repository.start_run({
            "import_key": str(uuid.uuid4()), "source_mode": "LOCAL_DB",
            "store_code": store_code, "inquiry_types": list(inquiry_types),
            "date_from": date_from, "date_to": date_to,
            "answered_only": answered_only, "require_seller_answer": require_seller_answer,
        })
        counts = self._empty_counts()
        cursor = 0
        try:
            while True:
                clauses, params = ["i.id>?"], [cursor]
                if store_code:
                    clauses.append("i.store_code=?"); params.append(store_code)
                if inquiry_types:
                    placeholders = ",".join("?" for _ in inquiry_types)
                    clauses.append(f"i.source_type IN ({placeholders})"); params.extend(inquiry_types)
                if date_from:
                    clauses.append("COALESCE(i.source_created_at,i.registered_at)>=?"); params.append(date_from)
                if date_to:
                    clauses.append("COALESCE(i.source_created_at,i.registered_at)<=?"); params.append(date_to)
                params.append(max(10, min(int(batch_size), 1000)))
                with self.database.connection() as connection:
                    rows = connection.execute(
                        f"""
                        SELECT i.*,
                          (SELECT COALESCE(le.seller_answer, le.final_answer)
                           FROM learning_examples le
                           WHERE le.inquiry_id=i.id AND le.learning_source='SELLER_ANSWER'
                           ORDER BY le.id DESC LIMIT 1) AS historical_seller_answer
                        FROM inquiries i WHERE {' AND '.join(clauses)}
                        ORDER BY i.id LIMIT ?
                        """,
                        tuple(params),
                    ).fetchall()
                if not rows:
                    break
                items: list[dict[str, Any]] = []
                for row in rows:
                    value = dict(row)
                    cursor = int(value["id"])
                    try:
                        raw = json.loads(value.get("raw_json") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        raw = {}
                    value.update({
                        "local_inquiry_id": value["id"],
                        "external_inquiry_id": value.get("source_question_id"),
                        "seller_answer": value.get("historical_seller_answer") or _seller_answer(raw),
                        "raw_payload": raw,
                    })
                    items.append(value)
                self._save_items(
                    items, counts=counts, require_seller_answer=require_seller_answer,
                    answered_only=answered_only, source_prefix="INQUIRY_DB",
                )
                counts["last_page"] += 1
                self.repository.update_run(int(run["id"]), **counts)
            status = "COMPLETED" if not counts["failed_count"] else "PARTIAL"
            return self.repository.update_run(
                int(run["id"]), status=status,
                completed_at=datetime.now(UTC).isoformat(timespec="milliseconds"), **counts,
            )
        except Exception as error:
            self.repository.update_run(
                int(run["id"]), status="FAILED", error_message=str(error)[:500],
                completed_at=datetime.now(UTC).isoformat(timespec="milliseconds"), **counts,
            )
            raise

    def import_naver(
        self, *, store_id: str | None = None,
        inquiry_types: Sequence[str] = SUPPORTED_INQUIRY_TYPES,
        from_datetime: datetime, to_datetime: datetime,
        answered_only: bool = True, require_seller_answer: bool = True,
        max_pages: int | None = None,
    ) -> dict[str, Any]:
        naver = self._naver_service()
        if not naver.settings.enabled:
            raise RuntimeError("네이버 문의 조회 설정이 비활성화되어 있습니다.")
        if from_datetime.tzinfo is None or to_datetime.tzinfo is None or from_datetime >= to_datetime:
            raise ValueError("조회 기간을 확인해 주세요.")
        targets = [get_store_config(store_id)] if store_id else get_configured_stores()
        run = self.repository.start_run({
            "import_key": str(uuid.uuid4()), "source_mode": "NAVER_API",
            "store_code": store_id, "inquiry_types": list(inquiry_types),
            "date_from": from_datetime.isoformat(), "date_to": to_datetime.isoformat(),
            "answered_only": answered_only, "require_seller_answer": require_seller_answer,
        })
        counts = self._empty_counts()
        page_limit = min(max_pages or naver.settings.max_pages, naver.settings.max_pages)
        try:
            for store in targets:
                token = naver._token(store)
                for inquiry_type in inquiry_types:
                  for window_from, window_to in self._date_windows(from_datetime, to_datetime):
                    previous: tuple[str, ...] | None = None
                    for page in range(1, page_limit + 1):
                        payload = naver._fetch_page(
                            inquiry_type, token=token, page=page,
                            from_datetime=window_from, to_datetime=window_to,
                        )
                        key = "contents" if inquiry_type == "PRODUCT_INQUIRY" else "content"
                        content = payload.get(key)
                        if not isinstance(content, list):
                            raise ValueError("네이버 문의 응답 형식을 확인할 수 없습니다.")
                        normalized: list[dict[str, Any]] = []
                        signature: list[str] = []
                        for raw in content:
                            if not isinstance(raw, dict):
                                counts["failed_count"] += 1
                                continue
                            item = naver._normalize(inquiry_type, raw, store_code=store.code)
                            signature.append(str(item.get("external_inquiry_id") or ""))
                            normalized.append(item)
                        current = tuple(signature)
                        if content and current == previous:
                            raise RuntimeError("네이버 과거 문의 페이지가 반복되어 가져오기를 중단했습니다.")
                        previous = current
                        self._save_items(
                            normalized, counts=counts,
                            require_seller_answer=require_seller_answer,
                            answered_only=answered_only, source_prefix="NAVER_API",
                        )
                        counts["last_page"] += 1
                        self.repository.update_run(int(run["id"]), **counts)
                        if not content or len(content) < naver.settings.page_size:
                            break
            status = "COMPLETED" if not counts["failed_count"] else "PARTIAL"
            return self.repository.update_run(
                int(run["id"]), status=status,
                completed_at=datetime.now(UTC).isoformat(timespec="milliseconds"), **counts,
            )
        except Exception as error:
            self.repository.update_run(
                int(run["id"]), status="FAILED", error_message=str(error)[:500],
                completed_at=datetime.now(UTC).isoformat(timespec="milliseconds"), **counts,
            )
            raise

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        left_tokens, right_tokens = set(TOKEN.findall(left)), set(TOKEN.findall(right))
        jaccard = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
        return 0.68 * jaccard + 0.32 * SequenceMatcher(None, left, right).ratio()

    def search(
        self, question: str, *, store_code: str | None = None,
        product_name: str | None = None, inquiry_type: str | None = None,
        limit: int = 4, include_risky: bool = False,
    ) -> list[dict[str, Any]]:
        query = normalize_learning_question(self.privacy.mask(question))
        ranked: list[tuple[float, dict[str, Any]]] = []
        for item in self.repository.candidates(store_code=store_code):
            risk = str(item.get("policy_risk") or "NONE").upper()
            if float(item.get("quality_score") or 0) < AUTO_REFERENCE_MIN_QUALITY:
                continue
            # This service feeds generation contexts.  Risky material remains
            # visible in the repository/admin UI but is never retrievable here.
            if risk in {"BLOCK", "BLOCKED", "HIGH"}:
                continue
            relevance = self._similarity(query, str(item.get("question_normalized") or ""))
            relevance += 0.08 if product_name and item.get("product_name") == self.privacy.mask(product_name) else 0
            relevance += 0.05 if inquiry_type and item.get("inquiry_type") == inquiry_type else 0
            relevance += float(item.get("quality_score") or 0) * 0.12
            # Preserve the legacy promotion score while all active Historical
            # employee answers participate as verified, opt-out Learning.
            relevance += 0.04 if item.get("promoted_learning_id") else 0
            if relevance >= 0.20:
                value = dict(item)
                value["relevance"] = round(relevance, 4)
                value["reference_strength"] = "HISTORICAL_VERIFIED_LEARNING"
                value["usage_notice"] = "과거 표현/대응 참고 전용. 현재 Rule·주문·DPS·상품DB·Template이 우선합니다."
                ranked.append((relevance, value))
        ranked.sort(
            key=lambda pair: (pair[0], pair[1].get("quality_score") or 0, pair[1].get("inquiry_created_at") or ""),
            reverse=True,
        )
        return [item for _, item in ranked[: max(1, min(int(limit), 5))]]

    def promote(self, case_id: int, *, actor: str) -> dict[str, Any]:
        case = self.repository.get(int(case_id))
        if not case:
            raise LookupError("Historical Case를 찾을 수 없습니다.")
        if not case.get("active") or float(case.get("quality_score") or 0) < 0.55:
            raise ValueError("활성 상태이고 품질 기준 0.55 이상인 사례만 승격할 수 있습니다.")
        if str(case.get("policy_risk") or "").upper() in {"HIGH", "BLOCK", "BLOCKED"}:
            raise ValueError("현재 정책과 충돌할 위험이 있는 사례는 승격할 수 없습니다.")
        if case.get("promoted_learning_id"):
            existing = LearningRepository(self.database).get_by_source_key(
                self._digest("HISTORICAL_PROMOTED", case["fingerprint"])
            )
            if existing:
                return existing
        from services.learning_service import LearningService
        saved = LearningService(self.database).capture_historical_promotion(
            case=case, actor=actor
        )
        self.repository.mark_promoted(int(case_id), int(saved["id"]))
        return saved
