# Q&A auto 통합 3단계: DPS 연동

## 작업 목적

기존 Samsung DPS Windows UI Automation을 변경하지 않고 문의 DB, workflow,
규칙 기반 답변 엔진에 연결했다. 실제 네이버 답변 등록, OpenAI 호출,
OTP 변경, 학습 자동 반영은 포함하지 않는다.

## 기존 DPS 구조 분석

- `services/dps_agent_client.py`: localhost Agent HTTP 호출, 상태 polling,
  연결/read timeout 분리
- `dps/agent_server.py`: 기존 Chrome DPS 탭 연결, 안전 검증, 조회 작업 관리
- `dps/dps_ui_automation.py`: 판매 → 온라인판매 → 구매요청리스트 이동,
  기간 클릭 선택, 주문번호 입력, 정확한 행 선택, 상세창 파싱·닫기
- `dps/context.py`, `dps/dates.py`: 주문 식별자와 조회 기간 계산
- `dps/identifiers.py`: 일반 `order_id`만 허용하고 상품주문번호 fallback 차단

셀러 ID 오입력 방지, Button/Hyperlink 조회 컨트롤 지원, 날짜 직접 입력 금지,
주문일 우선순위와 과거·현재·미래 월 제한은 기존 구현을 그대로 유지했다.

기존 DPS 결과는 Streamlit session에 머물러 통합 DB, workflow,
`AnswerRequest`에 연결되지 않았다. 이 단계에서는 자동화 내부를 리팩터링하지
않고 별도 integration 계층을 추가했다.

## 서비스 계층

```text
services/dps_lookup_policy.py
  └─ 조회 대상, 변경 요청, order_id, 질문 분할 판단

services/dps_enrichment_service.py
  ├─ 성공 캐시 조회
  ├─ dependency injection된 DPS client 호출
  ├─ 조회 이력 저장
  ├─ workflow와 activity log 갱신
  └─ AnswerRequest.metadata 주입

services/dps_result_normalizer.py
  └─ Agent 화면 구조를 중립 metadata와 오류 상태로 정규화
```

답변 엔진은 Agent, Chrome, UI Automation, SQLite를 import하지 않는다.
`services/answer_service.py`가 `AnswerRequest` 생성 직후 enrichment를
실행하고, 엔진은 중립적인 `metadata["dps"]`만 읽는다.

## 일반 문의와 배송 문의 분기

- 일반 문의: 제품 기능, 모델, 색상, 옵션, OTT, 구성, 정책 등은
  `NOT_REQUIRED`이며 repository와 Agent 호출이 모두 0회다.
- 배송·설치 현황: 일정, 지연, 출고·배송 상태, 기사 방문일 문의만 조회한다.
- 변경·요청: 설치일·배송일·주소 변경, 기사 연락처, 당일 확정 요청은 DPS
  결과가 있어도 `NEEDS_REVIEW`다.
- 혼합 문의: 문장 단위로 일반 질문과 DPS 질문을 추적한다. DPS 실패 시
  일반 답변은 유지하고 전체 결과만 `NEEDS_REVIEW`로 만든다.

## order_id 전용 정책

- DPS 입력값은 네이버 일반 `order_id`만 사용한다.
- `product_order_id`는 표시·네이버 상품주문 식별 용도다.
- 일반 주문번호가 없으면 Agent를 호출하지 않고
  `WAITING_FOR_ORDER_ID`와 직원 검토로 전환한다.
- integration client 호출 kwargs에는 `product_order_id`를 전달하지 않는다.
- 기존 identifier/context/Agent의 상품주문번호 fallback 차단 검증도 유지한다.

## DPS metadata

```json
{
  "dps": {
    "lookup_required": true,
    "lookup_status": "SUCCESS",
    "source": "DPS_AGENT",
    "order_id": "일반 주문번호",
    "sales_number": "DPS 판매번호",
    "delivery_status": "배송 준비 중",
    "installation_status": "설치 예정",
    "installation_date": "2026-08-03",
    "installation_date_text": "원문",
    "installation_time_text": null,
    "installation_type": null,
    "product_name": "모델 또는 상품명",
    "customer_region": null,
    "queried_at": "ISO 8601",
    "cache_used": false,
    "cache_age_seconds": 0,
    "elapsed_seconds": 53.2,
    "error_code": null,
    "error_message": null,
    "warnings": [],
    "change_request": false,
    "general_segments": [],
    "dps_segments": ["설치는 언제 오나요?"]
  }
}
```

빈 문자열, `-`, `없음`, `N/A`는 `null`로 정규화한다. 날짜는 가능한 경우
ISO 8601 날짜로 변환하며 원문이 필요하면 별도 필드에 보존한다.

## migration과 repository

migration v2에서 `dps_lookup_results`를 추가했다. 기존 v1과
`dps_results`는 수정하거나 삭제하지 않았다.

- inquiry, 일반 `order_id`, 통합 상태
- 개인정보 키를 제거한 Agent 결과
- 정규화 결과
- 상세 오류 코드와 사용자용 오류 메시지
- 조회·만료·생성·수정 시각
- order, inquiry, status, queried_at 인덱스
- inquiry 삭제 시 cascade

`repositories/dps_repository.py`가 생성, 최신 order/inquiry 조회, 최신 성공
조회, 이력, 실패 저장 및 캐시 유효성 검사를 담당한다. 실패 행은 이전 성공
행을 덮어쓰지 않는다.

## 캐시 정책

- SUCCESS: 기본 30분
- NOT_FOUND: 기본 5분
- timeout, offline, automation, parse 오류: 재사용하지 않음
- 강제 재조회: 캐시 무시
- 모든 실제 재조회 결과는 새 이력 행으로 저장
- posted 문의: 조회와 답변 재생성 차단

환경변수:

- `DPS_SUCCESS_CACHE_TTL_SECONDS`
- `DPS_NOT_FOUND_CACHE_TTL_SECONDS`

## timeout 정책

- connect: 기본 7초
- response/read: 기본 100초
- 전체 lookup: 기본 120초

환경변수:

- `DPS_CONNECT_TIMEOUT_SECONDS`
- `DPS_READ_TIMEOUT_SECONDS`
- `DPS_TOTAL_TIMEOUT_SECONDS`

read timeout 후 상태 polling은 전체 제한에서 이미 소비한 시간을 뺀
잔여 시간만 사용한다. timeout은 `AGENT_OFFLINE`이 아닌 `TIMEOUT`이다.

## 상태와 오류 코드

통합 상태:

`NOT_REQUIRED`, `WAITING_FOR_ORDER_ID`, `PENDING`, `RUNNING`, `SUCCESS`,
`NOT_FOUND`, `TIMEOUT`, `AGENT_OFFLINE`, `AUTOMATION_ERROR`, `PARSE_ERROR`,
`STALE_CACHE`, `CANCELLED`

원본 상세 코드는 별도 `error_code`에 보존한다.

- 서버 연결 실패: `AGENT_OFFLINE`
- connect/read/detail open timeout: `TIMEOUT`
- navigation, 주문 입력칸, 잘못된 필드, 상세창 열기·닫기: `AUTOMATION_ERROR`
- Agent JSON 또는 상세정보 parsing 실패: `PARSE_ERROR`
- 결과 없음 또는 정확한 order 행 없음: `NOT_FOUND`

사용자 화면과 workflow에는 한글 안내만 저장하고 traceback은 넣지 않는다.

## workflow 정책

- 조회 시작: `DPS_LOOKUP → RUNNING`, attempt 증가
- 성공·유효 캐시: `DPS_LOOKUP → COMPLETED`
- order_id 없음·NOT_FOUND: `DPS_LOOKUP → NEEDS_REVIEW`,
  inquiry `NEEDS_ATTENTION`
- TIMEOUT, AGENT_OFFLINE, 자동화·파싱 실패:
  `DPS_LOOKUP → FAILED`, inquiry `NEEDS_ATTENTION`
- 답변 정상 생성: `ANSWER_GENERATED → COMPLETED`,
  inquiry `REVIEW_PENDING`
- DPS 검토 필요: answer step `NEEDS_REVIEW`, inquiry `NEEDS_ATTENTION`

## Activity Log

다음 이벤트를 기록한다.

`DPS_LOOKUP_REQUESTED`, `DPS_CACHE_HIT`, `DPS_CACHE_MISS`,
`DPS_LOOKUP_STARTED`, `DPS_LOOKUP_SUCCEEDED`, `DPS_LOOKUP_NOT_FOUND`,
`DPS_LOOKUP_TIMED_OUT`, `DPS_AGENT_OFFLINE`, `DPS_LOOKUP_FAILED`,
`DPS_RETRY_REQUESTED`, `DPS_RESULT_INJECTED_TO_ANSWER`,
`ANSWER_GENERATED_WITH_DPS`, `ANSWER_REQUIRES_REVIEW_DUE_TO_DPS`

order ID는 기존 로그 repository에서 부분 마스킹된다. 전화번호, 주소,
고객명, 인증정보는 raw 저장 전 제거하고 로그 details에 넣지 않는다.

## 답변 정책

- SUCCESS: 배송 상태 또는 설치 예정일을 기존 답변 wrapper에 자연스럽게 결합
- 데이터 없는 SUCCESS 또는 부분/충돌 경고: 직원 검토
- NOT_FOUND, order_id 없음, timeout, offline, 자동화·파싱 실패:
  추측하지 않고 확인 안내와 `NEEDS_REVIEW`
- 변경 요청: 현재 일정은 안내할 수 있지만 변경 완료라고 답하지 않음
- mixed: 일반 답변 + DPS 확인 안내를 한 본문으로 결합하고 하위 질문 결과를
  metadata에 기록

실제 네이버 등록은 수행하지 않는다.

## UI 변경

기존 문의 상세 답변 영역에 다음을 추가했다.

- DPS 조회 필요 여부·상태·order_id·캐시·마지막 조회·소요시간
- 배송·설치 상태, 설치 예정일, DPS 판매번호, 오류·경고
- `DPS 조회`, `DPS 재조회`
- 배송 문의의 `DPS 결과를 반영하여 답변 초안 생성`

order_id가 없거나 posted이거나 실행 중이면 버튼이 비활성화된다. 기존의
고급 DPS 탭 연결·진단 화면과 자동화 기능은 제거하지 않았다.

## 테스트 결과

- 변경 전: 286 passed
- 신규: 32 passed
- 최종: 318 passed
- 실제 DPS, Chrome, 네트워크를 사용하는 단위 테스트 없음
- `compileall` 성공
- Streamlit health `HTTP 200: ok`

## Windows 검증

- Streamlit 별도 포트 18767에서 health 200 확인 후 종료
- 일반 문의: DPS client 0회, DPS 이력 0건, 답변 draft 1건
- 배송 문의: SUCCESS, 설치일 `2026-08-03` 답변 반영,
  `product_order_id` 미전달, DPS step COMPLETED
- 혼합 + timeout: 일반 OTT 답변 유지, 전체 NEEDS_REVIEW,
  DPS status TIMEOUT, 하위 질문 2건 추적
- 실제 Agent 상태: 실행 중이지만 `DPS_TAB_NOT_FOUND`,
  `connected=false`, `TAB_CLOSED`
- 연결된 DPS 탭이 없어 실조회는 안전상 실행하지 않음
- 현재 세션에 연결 가능한 브라우저 백엔드가 없어 화면 클릭 검증은 수행하지
  못했으며 health와 UI presenter 자동 테스트만 확인

## 알려진 제한과 기술 부채

- 정책 분류는 현재 명시적 한국어 패턴 기반이므로 표현 사전을 계속
  characterization해야 한다.
- 원본 DPS Agent는 큰 UI Automation 모듈이며 integration 오류 enum과 원본
  상세 코드가 별도 계층에 있다.
- 기존 session 기반 DPS 고급 화면과 새 DB 기반 답변 연동 화면이 함께 있어
  후속 UI 정리가 필요하다.
- STALE_CACHE와 CANCELLED는 schema와 표시를 지원하지만 현재 기본 흐름에서
  자동 생성되는 경우는 제한적이다.

## 다음 단계

직원 수정본과 DPS 근거를 함께 검토하는 승인 UI를 추가한 뒤, 승인된
`final_answer`만 네이버 등록 service에 전달한다. 등록 전에 posted 보호,
멱등성 키, 네이버 응답 이력과 실패 재시도 정책을 별도 단계로 구현해야 한다.
