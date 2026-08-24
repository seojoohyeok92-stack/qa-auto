from __future__ import annotations

import os


# 테스트가 운영 카카오 공통 대기열에 메시지를 넣지 않도록 기본 차단한다.
os.environ.setdefault("KAKAO_NOTIFY_ENABLED", "0")
