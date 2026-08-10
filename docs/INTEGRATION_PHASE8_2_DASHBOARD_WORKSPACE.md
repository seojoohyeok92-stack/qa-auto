# Phase 8.2 Dashboard Workspace Reconstruction 완료 보고서

검증 일시: 2026-07-30 (Asia/Seoul)

## 1. 수정 전 레이아웃 문제

- 답변 검토 영역이 문의 목록과 우측 카드가 끝난 뒤 전체 폭으로 렌더링되어 업무 흐름이 끊겼습니다.
- Program Answer, Staff Edit, Validator, Final Answer가 모두 세로로 펼쳐져 높이가 과도했습니다.
- 우측 상세/DPS 열과 답변 Workspace가 같은 행 구조를 공유하지 않아 빈 공간이 남았습니다.
- KPI, 상단 사용자 상태, 사이드바가 참고 이미지의 운영 콘솔 밀도와 달랐습니다.

## 2. 기준 이미지 분석 결과

`Dashboard.png`를 영역별로 분석해 다음 우선순위를 적용했습니다.

1. 고정형 그룹 사이드바
2. 헤더에 통합된 환경/잠금/알림/사용자 상태
3. 강한 색상 중심을 가진 5개 KPI
4. 좌측 문의 목록과 우측 상세/DPS
5. 좌측 하단 답변 Workspace와 우측 하단 진행 단계
6. 패널 내부 스크롤을 사용하는 조밀한 다크 운영 화면

## 3. 변경한 전체 Grid 구조

Streamlit DOM에서 CSS로 블록 순서를 이동하지 않고 Python 렌더링 순서를 변경했습니다.

```text
Topbar
KPI 5 cards
Filter
┌──────────────────────────┬────────────────┐
│ Inquiry list             │ Inquiry detail │
│                          │ DPS            │
├──────────────────────────┼────────────────┤
│ Review workspace         │ Workflow       │
└──────────────────────────┴────────────────┘
```

데스크톱 컬럼 비율은 `2.15 : 0.95`입니다. 순수 CSS Grid 대신 안정적인 두 행의 Streamlit columns/container를 사용하되 Python 순서와 최종 배치는 기준 구조와 동일하게 구성했습니다.

## 4. 답변 Workspace 이동 결과

답변 검토 및 승인을 전체 페이지 최하단 전체 폭에서 제거하고 문의 목록 바로 아래 왼쪽 업무 열로 이동했습니다. 우측 진행 카드의 왼쪽 공간을 사용하며 높이를 455px로 제한했습니다.

## 5. Program Answer/Staff Edit 구조

Segmented control로 다음 중 하나만 표시합니다.

- Program Answer
- 직원 수정본
- Final Answer

답변 TextArea는 항상 하나만 렌더링하며 높이는 220px, 내부 스크롤 방식입니다. 직원 수정본의 기존 자동 저장과 임시 저장 동작을 유지했습니다.

## 6. Validator 표시 구조

세로 단계 표시를 제거하고 답변 카드 하단의 39px 상태 바로 변경했습니다.

- `Validator 통과`
- `Validator 확인 필요`

상세 GPT/Validator 정보는 expander 안에서만 표시합니다.

## 7. 오른쪽 패널 구성

- 문의 상세: 235px 내부 스크롤, 기존 답변 expander, 주문 조회 버튼
- DPS 정보: 245px 내부 스크롤, 일반 order_id 정책과 조회 버튼 유지
- 진행 단계: 455px 내부 스크롤, 7단계 상태와 Activity Log 연결

마지막 진행 단계는 항상 `네이버 등록 — 잠금`입니다.

## 8. KPI 시각 개선

- 파랑/초록/주황/보라/빨강 상태 클래스
- 68px 원형 입체 아이콘
- 자체 inline SVG 아이콘
- radial gradient와 glow/shadow
- 큰 수치, 오늘/전체 수치
- 실제 최근 7일 데이터 기반 SVG sparkline
- 데이터가 없을 때 `최근 7일 추세 데이터 없음`
- 카드 전체 클릭 영역과 hover 효과

거짓 추세 데이터는 생성하지 않습니다.

## 9. 사이드바 개선

상단 `OJE / Q&A auto` 브랜드와 다음 그룹을 구성했습니다.

- 문의 관리: 신규 문의, 전체 문의, 문의 검색
- 진행 관리: 진행 카드, 답변 초안, 검토 대기
- 시스템 관리: DPS 관리, 설정 관리, 활동 로그, UAT 관리자 진단

하단 시스템 상태 카드는 DB health, DPS Agent status, 네이버 설정 스토어 수, GPT Provider/API key 준비 상태, DPS Chrome 연결 상태를 실제 설정/진단 결과로 계산합니다.

## 10. 상단 상태 바

- 환경: `QNA_ENVIRONMENT` 또는 GPT 실행 모드 기반 운영/테스트
- 네이버 등록 잠금
- 실제 오류/주의 집계 알림 수
- 현재 session identity의 사용자명과 역할
- 날짜 범위 필터

고정 사용자명이나 고정 알림 수를 제거했습니다.

## 11. 문의 목록 페이지네이션

- 기본 10건
- 10/20/50건 선택
- 이전/다음
- 현재/전체 페이지 표시
- 필터 변경 시 1페이지 초기화
- 페이지 범위 보정
- 현재 페이지에 없는 선택 문의는 안전하게 초기화

목록은 문의 ID 클릭으로 선택하며 `상세 보기` 버튼 줄바꿈 문제를 제거했습니다.

## 12. 첫 화면 표시 영역

1920×1080 기준 목표 높이:

- KPI: 158px
- 문의 목록: 390px
- 문의 상세/DPS: 235px/245px
- 답변 Workspace/진행 단계: 455px

실제 DB AppTest에서 첫 페이지 문의 10행, 문의 상세, DPS, 답변 Workspace 상단, 진행 단계가 모두 동일 Dashboard DOM에 렌더링됨을 확인했습니다.

## 13. 전체 스크롤 감소 결과

- 목록: 페이지 전체가 아닌 390px 내부 스크롤
- 문의 상세/DPS: 각 카드 내부 스크롤
- 답변 본문: 하나의 220px TextArea
- 답변 Workspace/진행 단계: 각 455px 내부 스크롤

기존의 3개 답변 TextArea와 6개 세로 단계 체인을 제거해 답변 영역의 렌더링 높이를 크게 줄였습니다.

## 14. 수정 파일

- `app.py`
- `ui/dashboard.py`
- `ui/dashboard.css`
- `ui/sidebar.py`
- `ui/review_workspace.py`
- `tests/test_phase8_1_dashboard_hotfix.py`
- `tests/test_phase8_2_dashboard_workspace.py`
- `README.md`
- `CHANGELOG.md`

## 15. 신규 컴포넌트

기존 프로젝트의 import 경로와 테스트 호환성을 유지하기 위해 새 패키지를 만들지 않고 다음 재사용 컴포넌트를 분리했습니다.

- `render_header`: 실제 상태 기반 운영 Topbar
- `_kpi_icon_svg`, `_sparkline_svg`, `_seven_day_counts`
- `sidebar_system_status`, `_cached_dps_status`, `_menu_button`
- `paginate_items`, `_render_pagination`
- compact answer analysis/meta/validator UI

비즈니스 서비스는 기존 Sync, Order, DPS, Answer, Approval, Activity Log 서비스를 그대로 호출합니다.

## 16. 추가 테스트

Phase 8.2 전용 테스트 9개를 추가했습니다.

- Workspace Python 렌더링 순서
- Program/직원 수정/Final 전환과 TextArea 1개
- Validator compact bar와 세로 chain 제거
- KPI SVG/상태 클래스/실데이터 추세 fallback
- 실제 사용자/알림 기반 Topbar
- 그룹형 사이드바와 실제 진단값
- 페이지네이션 계산/렌더링/전환
- Desktop/fallback CSS
- AppTest 렌더링 예외 없음

## 17. 전체 테스트 결과

`684 passed in 42.10s`

자동 테스트에서는 실제 네이버 API, DPS, GPT를 호출하지 않았습니다.

## 18. Windows 실제 검증 결과

- Streamlit 서버: 정상 기동
- health endpoint: `ok`
- 서버 stderr: 애플리케이션 오류 없음
- 실제 DB AppTest: 예외 0
- 첫 페이지 문의 행: 10개
- 답변 TextArea: 1개
- 답변 segmented control: 1개
- Topbar/시스템 상태/상세/DPS/Workspace/진행/잠금: 모두 확인
- 연결 가능한 브라우저가 없어 1920×1080 자동 캡처는 생성하지 못했습니다.

## 19. 기준 이미지와 남은 차이

- Streamlit의 컬럼 DOM 안정성을 위해 기준 이미지의 순수 CSS Grid 대신 두 행 nested columns를 사용했습니다.
- 문의 선택은 행 전체 클릭 대신 접근 가능한 문의 ID 버튼을 사용합니다.
- 실제 브라우저 픽셀 비교는 자동 캡처를 사용할 수 없어 수행하지 못했습니다.
- 기준 이미지의 장식용 3D 래스터 아이콘 대신 저작권 문제가 없는 자체 SVG/CSS 입체 아이콘을 사용했습니다.

## 20. 기능 퇴행 여부

동기화, 화면 새로고침, 주문 조회, DPS 일반 order_id 정책, GPT 수동 생성, Staff Edit, Validator, Approval, Final Answer, Activity Log, KPI/문의 필터, 검색, 선택 유지, UAT 및 기존 DB 기능을 유지했습니다. 전체 684개 테스트가 통과했습니다.

## 21. 네이버 등록 잠금 유지 확인

- Topbar: `네이버 등록 잠금`
- Sidebar 시스템 카드: `네이버 등록 — 잠금`
- 답변 Workspace: 승인은 Final Answer만 생성
- 진행 단계: 마지막 단계 항상 잠금
- 실제 네이버 등록 API 호출 없음

## 22. Phase 9 미수행 확인

Phase 9 GPT-First 구조 또는 관련 비즈니스 로직 변경은 수행하지 않았습니다.

## 수동 확인 체크리스트

Chrome 1920×1080, 기본 확대율에서 다음을 확인합니다.

1. KPI 아이콘과 카드 glow가 과도하지 않은지
2. 목록 5~7행과 답변 Workspace 상단이 첫 화면에 함께 보이는지
3. 문의 ID 클릭 시 선택 강조와 오른쪽 상세가 갱신되는지
4. Program Answer/직원 수정본/Final Answer 전환 시 본문 하나만 보이는지
5. 상세/DPS/답변/진행 카드 내부 스크롤에서 버튼이 잘리지 않는지
6. 1200px 미만 화면에서 세로 fallback이 읽기 쉬운지

실제 API Key, Client Secret, OTP, 고객 개인정보는 보고서에 포함하지 않았습니다.
