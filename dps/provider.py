import hashlib
from datetime import date, datetime, timedelta
from typing import Protocol

from dps.models import InstallationInfo


class DpsProvider(Protocol):
    """실제 DPS provider가 구현해야 하는 최소 조회 인터페이스입니다."""

    def lookup(
        self,
        *,
        order_number: str,
        number_type: str,
    ) -> InstallationInfo:
        """주문 참조값으로 표준 설치정보를 반환합니다."""


class DeterministicDpsProvider:
    """외부 요청 없이 주문번호에 따라 일정한 모의 결과를 반환합니다."""

    def lookup(
        self,
        *,
        order_number: str,
        number_type: str,
    ) -> InstallationInfo:
        del number_type

        digest = hashlib.sha256(
            order_number.encode("utf-8")
        ).digest()
        scenario = digest[0] % 3
        today = date.today()
        queried_at = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )

        if scenario == 0:
            return InstallationInfo(
                success=True,
                installation_status="설치 완료",
                scheduled_date=(
                    today - timedelta(days=1)
                ).isoformat(),
                completed_date=today.isoformat(),
                assignment_status="배정 완료",
                technician_assigned=True,
                technician_name=None,
                visit_time_window="방문 완료",
                installation_memo=(
                    "모의 데이터: 설치 완료 상태입니다."
                ),
                source_order_number=order_number,
                queried_at=queried_at,
            )

        if scenario == 1:
            return InstallationInfo(
                success=True,
                installation_status="설치 예정",
                scheduled_date=(
                    today + timedelta(days=3)
                ).isoformat(),
                completed_date=None,
                assignment_status="배정 완료",
                technician_assigned=True,
                technician_name=None,
                visit_time_window="오후 13:00~17:00",
                installation_memo=(
                    "모의 데이터: 방문 전 일정 확인이 필요합니다."
                ),
                source_order_number=order_number,
                queried_at=queried_at,
            )

        return InstallationInfo(
            success=True,
            installation_status="설치 일정 확인 중",
            scheduled_date=None,
            completed_date=None,
            assignment_status="미배정",
            technician_assigned=False,
            technician_name=None,
            visit_time_window=None,
            installation_memo=(
                "모의 데이터: 확정된 설치 일정이 없습니다."
            ),
            source_order_number=order_number,
            queried_at=queried_at,
        )
