from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from enum import Enum

import bcrypt

from repositories.database import Database
from repositories.local_user_repository import LocalUserRepository
from repositories.log_repository import LogRepository
from uat.models import UserRole


class Permission(str, Enum):
    SETTINGS_DIAGNOSTICS = "SETTINGS_DIAGNOSTICS"
    ENV_COMPARE = "ENV_COMPARE"
    USER_MANAGE = "USER_MANAGE"
    GPT_GOVERNANCE_VIEW = "GPT_GOVERNANCE_VIEW"
    ACTIVITY_LOG_FULL = "ACTIVITY_LOG_FULL"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    SYSTEM_STATS = "SYSTEM_STATS"
    INQUIRY_VIEW = "INQUIRY_VIEW"
    STAFF_EDIT = "STAFF_EDIT"
    DPS_LOOKUP = "DPS_LOOKUP"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"


ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.ADMIN: frozenset(Permission),
    UserRole.MANAGER: frozenset(
        {
            Permission.INQUIRY_VIEW,
            Permission.STAFF_EDIT,
            Permission.DPS_LOOKUP,
            Permission.APPROVE,
            Permission.REJECT,
            Permission.SYSTEM_STATS,
            Permission.GPT_GOVERNANCE_VIEW,
            Permission.ACTIVITY_LOG_FULL,
        }
    ),
    UserRole.AGENT: frozenset(
        {
            Permission.INQUIRY_VIEW,
            Permission.STAFF_EDIT,
            Permission.DPS_LOOKUP,
            Permission.APPROVAL_REQUEST,
        }
    ),
}


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    username: str
    display_name: str
    role: UserRole
    force_password_change: bool

    def can(self, permission: Permission | str) -> bool:
        return Permission(permission) in ROLE_PERMISSIONS[self.role]


class AuthenticationError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


class LocalAuthService:
    def __init__(self, database: Database) -> None:
        self.users = LocalUserRepository(database)
        self.logs = LogRepository(database)

    @staticmethod
    def hash_password(password: str) -> str:
        value = str(password)
        if len(value) < 10:
            raise ValueError("비밀번호는 10자 이상이어야 합니다.")
        return bcrypt.hashpw(value.encode("utf-8"), bcrypt.gensalt()).decode(
            "ascii"
        )

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(
                str(password).encode("utf-8"),
                str(password_hash).encode("ascii"),
            )
        except (ValueError, TypeError):
            return False

    def bootstrap_admin(
        self, *, username: str, display_name: str, password: str
    ) -> dict:
        if self.users.list_users():
            raise ValueError("초기 관리자는 사용자 테이블이 비어 있을 때만 생성됩니다.")
        user = self.users.create(
            username=username,
            display_name=display_name,
            password_hash=self.hash_password(password),
            role=UserRole.ADMIN,
            force_password_change=True,
        )
        self.logs.record_system(
            "LOCAL_ADMIN_CREATED",
            "로컬 초기 관리자를 생성했습니다.",
            details={"actor": user["username"], "role": "ADMIN"},
        )
        return user

    def authenticate(self, username: str, password: str) -> AuthenticatedUser:
        correlation_id = str(uuid.uuid4())
        row = self.users.get_by_username(username)
        if row is None or not bool(row["active"]):
            self.users.record_login(
                username=username,
                success=False,
                event_code="LOGIN_FAILED",
                reason_code="UNKNOWN_OR_DISABLED_USER",
                correlation_id=correlation_id,
            )
            raise AuthenticationError("사용자명 또는 비밀번호를 확인해 주세요.")
        if not self.verify_password(password, row["password_hash"]):
            self.users.record_login(
                username=username,
                user_id=int(row["id"]),
                success=False,
                event_code="LOGIN_FAILED",
                reason_code="INVALID_PASSWORD",
                correlation_id=correlation_id,
            )
            raise AuthenticationError("사용자명 또는 비밀번호를 확인해 주세요.")
        self.users.record_login(
            username=username,
            user_id=int(row["id"]),
            success=True,
            event_code="LOGIN_SUCCEEDED",
            correlation_id=correlation_id,
        )
        return AuthenticatedUser(
            id=int(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=UserRole(row["role"]),
            force_password_change=bool(row["force_password_change"]),
        )

    def change_password(
        self, user: AuthenticatedUser, old_password: str, new_password: str
    ) -> None:
        row = self.users.get_by_id(user.id)
        if row is None or not self.verify_password(
            old_password, row["password_hash"]
        ):
            raise AuthenticationError("현재 비밀번호가 올바르지 않습니다.")
        self.users.update_password(user.id, self.hash_password(new_password))
        self.logs.record_system(
            "LOCAL_PASSWORD_CHANGED",
            "로컬 사용자 비밀번호가 변경되었습니다.",
            details={"actor": user.username, "role": user.role.value},
        )

    @staticmethod
    def require(
        user: AuthenticatedUser, permission: Permission | str
    ) -> None:
        if not user.can(permission):
            raise AuthorizationError("현재 역할에는 이 작업 권한이 없습니다.")


def local_auth_enabled() -> bool:
    return os.getenv("QNA_LOCAL_AUTH_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }

