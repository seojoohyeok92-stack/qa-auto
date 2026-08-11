from __future__ import annotations

from pathlib import Path

import pytest

from repositories.database import Database
from repositories.local_user_repository import LocalUserRepository
from repositories.quality_metric_repository import QualityMetricRepository
from services.approval_service import ApprovalService
from services.local_auth_service import (
    AuthenticatedUser,
    AuthenticationError,
    AuthorizationError,
    LocalAuthService,
    Permission,
)
from services.quality_metrics_service import QualityMetricsService
from uat.models import UserRole


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "phase7.db")
    db.initialize()
    return db


def seed_draft(database: Database, *, posted: bool = False) -> tuple[int, int]:
    with database.transaction() as connection:
        inquiry_id = int(
            connection.execute(
                """
                INSERT INTO inquiries (
                    store_code, source_type, source_question_id,
                    inquiry_type, content, post_status
                ) VALUES ('STORE','CUSTOMER_INQUIRY','q1','배송','문의',?)
                """,
                ("POSTED" if posted else "NOT_POSTED",),
            ).lastrowid
        )
        draft_id = int(
            connection.execute(
                """
                INSERT INTO answer_drafts (
                    inquiry_id, program_status, category, provider,
                    original_answer, review_status, posted
                ) VALUES (?, 'GENERATED', '배송', 'rules', ?, 'PENDING', ?)
                """,
                (inquiry_id, "배송 준비 중이며 2026년 8월 3일 예정입니다.", int(posted)),
            ).lastrowid
        )
    return inquiry_id, draft_id


def test_migration_v6_creates_operational_tables(database: Database) -> None:
    assert database.migration_versions() == list(range(1, 24))
    with database.connection() as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "local_users", "login_audit", "quality_metrics", "uat_runs",
        "environment_check_runs", "env_comparison_runs",
    }.issubset(names)


def test_migration_v6_is_reentrant(database: Database) -> None:
    assert database.initialize() == []


def test_password_hash_is_not_plaintext() -> None:
    hashed = LocalAuthService.hash_password("long-password")
    assert hashed != "long-password"
    assert hashed.startswith("$2")


def test_short_password_is_rejected() -> None:
    with pytest.raises(ValueError, match="10자"):
        LocalAuthService.hash_password("short")


def test_bootstrap_admin_and_force_change(database: Database) -> None:
    user = LocalAuthService(database).bootstrap_admin(
        username="admin", display_name="관리자", password="long-password"
    )
    assert user["role"] == "ADMIN"
    assert bool(user["force_password_change"])
    assert "long-password" not in str(user)


def test_second_bootstrap_is_rejected(database: Database) -> None:
    service = LocalAuthService(database)
    service.bootstrap_admin(
        username="admin", display_name="관리자", password="long-password"
    )
    with pytest.raises(ValueError):
        service.bootstrap_admin(
            username="other", display_name="다른 관리자", password="other-password"
        )


def test_authentication_success_records_audit(database: Database) -> None:
    service = LocalAuthService(database)
    service.bootstrap_admin(
        username="admin", display_name="관리자", password="long-password"
    )
    user = service.authenticate("admin", "long-password")
    assert user.role is UserRole.ADMIN
    with database.connection() as connection:
        audit = connection.execute(
            "SELECT * FROM login_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert audit["success"] == 1


def test_authentication_failure_does_not_store_password(database: Database) -> None:
    service = LocalAuthService(database)
    service.bootstrap_admin(
        username="admin", display_name="관리자", password="long-password"
    )
    with pytest.raises(AuthenticationError):
        service.authenticate("admin", "wrong-password")
    with database.connection() as connection:
        text = str(
            dict(
                connection.execute(
                    "SELECT * FROM login_audit ORDER BY id DESC LIMIT 1"
                ).fetchone()
            )
        )
    assert "wrong-password" not in text


def test_change_password_clears_force_change(database: Database) -> None:
    service = LocalAuthService(database)
    row = service.bootstrap_admin(
        username="admin", display_name="관리자", password="long-password"
    )
    user = service.authenticate("admin", "long-password")
    service.change_password(user, "long-password", "new-long-password")
    updated = LocalUserRepository(database).get_by_id(int(row["id"]))
    assert updated and not bool(updated["force_password_change"])
    assert service.authenticate("admin", "new-long-password")


@pytest.mark.parametrize(
    ("role", "permission", "allowed"),
    [
        (UserRole.ADMIN, Permission.USER_MANAGE, True),
        (UserRole.ADMIN, Permission.APPROVE, True),
        (UserRole.MANAGER, Permission.APPROVE, True),
        (UserRole.MANAGER, Permission.ENV_COMPARE, False),
        (UserRole.AGENT, Permission.STAFF_EDIT, True),
        (UserRole.AGENT, Permission.DPS_LOOKUP, True),
        (UserRole.AGENT, Permission.APPROVE, False),
        (UserRole.AGENT, Permission.ACTIVITY_LOG_FULL, False),
    ],
)
def test_role_permission_matrix(
    role: UserRole, permission: Permission, allowed: bool
) -> None:
    user = AuthenticatedUser(1, "user", "사용자", role, False)
    assert user.can(permission) is allowed


def test_require_rejects_forbidden_action() -> None:
    user = AuthenticatedUser(1, "agent", "상담원", UserRole.AGENT, False)
    with pytest.raises(AuthorizationError):
        LocalAuthService.require(user, Permission.APPROVE)


@pytest.mark.parametrize(
    ("original", "edited", "char_changed", "word_changed"),
    [
        ("안녕하세요.", "안녕하세요.", 0.0, 0.0),
        ("배송 준비 중입니다.", "배송이 준비 중입니다.", True, True),
        ("", "", 0.0, 0.0),
        ("한글 답변입니다.", "한글 수정 답변입니다.", True, True),
    ],
)
def test_quality_change_ratios(
    original: str, edited: str, char_changed, word_changed
) -> None:
    metric = QualityMetricsService().calculate(original, edited)
    assert (metric.character_change_ratio > 0) is bool(char_changed)
    assert (metric.word_change_ratio > 0) is bool(word_changed)


def test_quality_sentence_add_delete() -> None:
    metric = QualityMetricsService().calculate(
        "첫 문장입니다. 둘째 문장입니다.",
        "첫 문장입니다. 새 문장입니다. 마지막 문장입니다.",
    )
    assert metric.sentences_added >= 1
    assert metric.sentences_deleted >= 1


def test_quality_fact_change_detects_date() -> None:
    metric = QualityMetricsService().calculate(
        "설치일은 2026-08-03입니다.", "설치일은 2026-08-04입니다."
    )
    assert metric.fact_changed


def test_quality_tone_only_change() -> None:
    metric = QualityMetricsService().calculate(
        "배송 준비 중입니다.", "안녕하세요. 배송 준비 중입니다. 감사합니다."
    )
    assert metric.tone_changed
    assert not metric.fact_changed


def test_quality_prohibited_expression_change() -> None:
    metric = QualityMetricsService().calculate(
        "무조건 배송됩니다.", "배송 여부를 확인하겠습니다."
    )
    assert metric.prohibited_expression_changed


def test_quality_metric_store_and_read(database: Database) -> None:
    inquiry_id, draft_id = seed_draft(database)
    stored = QualityMetricsService(database).calculate_and_store(
        inquiry_id=inquiry_id,
        answer_draft_id=draft_id,
        actor="manager",
        category="배송",
        program_answer="배송 준비 중입니다.",
        staff_answer="현재 배송 준비 중입니다.",
        approved=False,
    )
    assert stored["details_json"]["not_ground_truth_score"] is True
    assert QualityMetricRepository(database).latest_for_draft(draft_id)


def test_approval_records_actor_and_quality(database: Database) -> None:
    inquiry_id, draft_id = seed_draft(database)
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft_id,
        edited_answer="직원 수정 답변입니다.",
        actor="manager-one",
    )
    service.approve(
        inquiry_id=inquiry_id, draft_id=draft_id, actor="manager-one"
    )
    with database.connection() as connection:
        actor = connection.execute(
            "SELECT actor FROM approval_history WHERE action='APPROVED'"
        ).fetchone()[0]
        approved_metric = connection.execute(
            "SELECT approved FROM quality_metrics ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert actor == "manager-one"
    assert approved_metric == 1


def test_posted_protection_still_blocks_edit(database: Database) -> None:
    inquiry_id, draft_id = seed_draft(database, posted=True)
    with pytest.raises(Exception, match="등록 완료"):
        ApprovalService(database).save_edited_answer(
            inquiry_id=inquiry_id,
            draft_id=draft_id,
            edited_answer="변경 금지",
            actor="agent",
        )
