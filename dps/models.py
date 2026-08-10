from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class InstallationInfo:
    """UI와 향후 AI 연계에서 공통으로 사용할 표준 설치정보입니다."""

    success: bool
    installation_status: str
    scheduled_date: str | None
    completed_date: str | None
    assignment_status: str
    technician_assigned: bool | None
    technician_name: str | None
    visit_time_window: str | None
    installation_memo: str | None
    source_order_number: str | None
    queried_at: str
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Streamlit session state에 저장 가능한 사전으로 변환합니다."""

        return asdict(self)
