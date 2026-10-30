"""What was generated for an inquiry, and what the marketplace now holds.

Once a person answered in Naver's or Coupang's seller centre, the next sync
marked the inquiry answered and the answer panel turned to the market's reply.
On Coupang the panel then became read-only and dropped the program answer
altogether, so the generated text looked lost. It never was: ``answer_drafts``
rows are only ever inserted, ``original_answer`` is never updated, a staff edit
goes to ``edited_answer`` on the same row and a regeneration is a new row. The
26.10.30 server copy holds 799 Naver and 34 Coupang inquiries answered outside
Q&A Auto whose generated draft is still stored.

This reads those rows back and labels each one by what the stored record can
prove: which row came first and which last, which one Q&A Auto registered and
who did it, and whether a person saved an edit here. A difference between the
generated text and the market's reply is shown, never explained -- nothing
stored says who changed a reply in a seller centre, so the label does not
either. Pure: no query, no write, no provider call.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from repositories.answer_repository import AnswerRepository
from services.answer_service import is_valid_draft

SYSTEM_AUTO_POST_ACTOR = "SYSTEM_AUTO_POST"

FIRST_GENERATED = "최초 생성 답변"
LATEST_GENERATED = "최신 생성 답변"
POSTED_DRAFT = "등록에 사용된 초안"
STAFF_EDIT = "직원 수정본"
SENT_ANSWER = "Q&A Auto 전송 답변"
MARKET_ANSWER = "마켓 등록 답변"
MARKET_ANSWER_NOTE = "마켓에서 조회한 현재 답변 · 작성자 구분 정보 없음"

NO_GENERATED_ANSWER = "생성된 프로그램 답변 없음"


@dataclass(frozen=True)
class HistoryEntry:
    label: str
    body: str
    note: str
    draft_id: int | None = None


@dataclass(frozen=True)
class AnswerHistory:
    entries: tuple[HistoryEntry, ...]
    generated_count: int
    #: The generated text the market reply is compared with, and its label.
    compared_generated: HistoryEntry | None
    market_answer: str
    registration: str
    same_text: bool | None
    #: Whether the market's reply is still the text Q&A Auto sent; ``None``
    #: when Q&A Auto sent nothing or the reply was not read back.
    market_matches_sent: bool | None = None

    @property
    def has_generated(self) -> bool:
        return self.generated_count > 0


def _ordered(drafts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first: ``created_at`` then the autoincrement id."""

    rows = [row for row in drafts if isinstance(row, dict)]
    return sorted(
        rows,
        key=lambda row: (str(row.get("created_at") or ""), int(row.get("id") or 0)),
    )


def _comparable(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def posted_draft(
    inquiry: dict[str, Any], drafts: Iterable[dict[str, Any]]
) -> dict[str, Any] | None:
    """The draft Q&A Auto registered, if it registered one."""

    rows = _ordered(drafts)
    posted_id = inquiry.get("posted_draft_id")
    if posted_id is not None:
        for row in rows:
            if int(row.get("id") or 0) == int(posted_id):
                return row
    posted = [row for row in rows if row.get("posted")]
    return posted[-1] if posted else None


def registration_label(
    inquiry: dict[str, Any], drafts: Iterable[dict[str, Any]]
) -> str:
    """Who registered the reply, as far as Q&A Auto recorded it.

    ``post_status`` is not used: a sync that first sees an inquiry already
    answered writes POSTED with no actor, so it cannot tell Q&A Auto's own
    registration from anyone else's.
    """

    row = posted_draft(inquiry, drafts)
    if row is not None:
        actor = str(inquiry.get("post_actor") or "").strip()
        if actor == SYSTEM_AUTO_POST_ACTOR:
            return f"Q&A Auto 자동등록 · Draft #{row['id']}"
        return (
            f"Q&A Auto 직원 등록 ({actor or '등록자 미기록'}) · "
            f"Draft #{row['id']}"
        )
    if inquiry.get("source_answered"):
        return "Q&A Auto 등록 기록 없음 · 마켓에서 답변완료로 조회됨"
    return "미등록"


def build_answer_history(
    inquiry: dict[str, Any],
    drafts: Iterable[dict[str, Any]],
    *,
    market_answer: str = "",
    source_label: Any = lambda draft: str(draft.get("source") or "-"),
    format_time: Any = None,
) -> AnswerHistory:
    """Every stored answer for one inquiry, labelled by what the record shows.

    ``drafts`` is the inquiry's ``answer_drafts`` rows in any order. A row
    whose generation produced no text is not a generated answer and is left
    out rather than shown empty.
    """

    rows = _ordered(drafts)
    generated = [row for row in rows if is_valid_draft(row.get("original_answer"))]
    fmt = format_time or (lambda value: str(value or "").strip() or "시각 미기록")

    def note(row: dict[str, Any]) -> str:
        return (
            f"Draft #{row['id']} · 출처 {source_label(row)} · "
            f"생성 {fmt(row.get('created_at'))}"
        )

    entries: list[HistoryEntry] = []
    seen: set[int] = set()
    if generated:
        first = generated[0]
        entries.append(HistoryEntry(
            FIRST_GENERATED, str(first["original_answer"]), note(first),
            int(first["id"]),
        ))
        seen.add(int(first["id"]))
        latest = generated[-1]
        if int(latest["id"]) not in seen:
            entries.append(HistoryEntry(
                LATEST_GENERATED, str(latest["original_answer"]),
                note(latest) + f" · 생성 이력 {len(generated)}건",
                int(latest["id"]),
            ))
            seen.add(int(latest["id"]))

    registered = posted_draft(inquiry, rows)
    if (
        registered is not None
        and int(registered["id"]) not in seen
        and is_valid_draft(registered.get("original_answer"))
    ):
        entries.append(HistoryEntry(
            POSTED_DRAFT, str(registered["original_answer"]), note(registered),
            int(registered["id"]),
        ))
        seen.add(int(registered["id"]))

    # A staff edit saved here lives on the row it edited. The registered row's
    # edit is the one that went out; otherwise the newest saved edit.
    edited_rows = [row for row in rows if is_valid_draft(row.get("edited_answer"))]
    edited = (
        registered
        if registered is not None and is_valid_draft(registered.get("edited_answer"))
        else edited_rows[-1] if edited_rows else None
    )
    if edited is not None:
        entries.append(HistoryEntry(
            STAFF_EDIT, str(edited["edited_answer"]),
            f"Draft #{edited['id']} 에 Q&A Auto 대시보드에서 저장된 수정본 · "
            f"마지막 변경 {fmt(edited.get('updated_at'))}",
            int(edited["id"]),
        ))

    registration = registration_label(inquiry, rows)
    # What Q&A Auto actually sent, chosen by the same rule the post paths use:
    # automatic posting sends ``final_answer``, which auto-finalisation may
    # have extended past the generated text (11407 gained its closing
    # paragraph there). Listed only when it is not already one of the above.
    sent = ""
    if registered is not None:
        manual = str(inquiry.get("post_actor") or "") != SYSTEM_AUTO_POST_ACTOR
        field, sent = AnswerRepository.posting_answer(registered, manual=manual)
        if sent and _comparable(sent) not in {
            _comparable(entry.body) for entry in entries
            if entry.draft_id == int(registered["id"])
        }:
            entries.append(HistoryEntry(
                SENT_ANSWER, sent,
                f"Draft #{registered['id']} 의 {field} · {registration}",
                int(registered["id"]),
            ))
    market = str(market_answer or "").strip()
    if market:
        entries.append(HistoryEntry(MARKET_ANSWER, market, MARKET_ANSWER_NOTE))

    # Compare the market reply with the text that would have gone out: the
    # registered row when Q&A Auto registered one, the newest generation
    # otherwise.
    compared: HistoryEntry | None = None
    if registered is not None and is_valid_draft(registered.get("original_answer")):
        compared = next(
            entry for entry in entries if entry.draft_id == int(registered["id"])
            and entry.label != STAFF_EDIT
        )
    elif generated:
        compared = next(
            entry for entry in entries
            if entry.draft_id == int(generated[-1]["id"])
            and entry.label != STAFF_EDIT
        )
    same = (
        _comparable(compared.body) == _comparable(market)
        if compared is not None and market
        else None
    )
    return AnswerHistory(
        entries=tuple(entries),
        generated_count=len(generated),
        compared_generated=compared,
        market_answer=market,
        registration=registration,
        same_text=same,
        market_matches_sent=(
            _comparable(sent) == _comparable(market) if sent and market else None
        ),
    )
