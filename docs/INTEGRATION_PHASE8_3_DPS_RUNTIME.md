# Phase 8.3 Dashboard DPS Runtime Fix 완료 보고서

검증 일시: 2026-07-30 (Asia/Seoul)

## 1. 실제 원인

Dashboard의 DPS 버튼은 공통 orchestrator를 호출하고 있었지만, orchestrator 아래의
`DpsEnrichmentService`가 답변 생성용 문의 문장 정책을 다시 적용했습니다. 문의 문장에
배송·설치 키워드가 없으면 `NOT_REQUIRED`로 종료되어 Agent `/lookup`이 호출되지 않았고,
따라서 DB 결과와 Dashboard 카드도 계속 `조회 전`으로 남았습니다.

추가로 주문 조회 결과가 session에만 남고 일반 `order_id`와 실제 `order_date`가 inquiry
DB에 저장되지 않았으며, 일부 경로는 문의 접수일을 주문일로 대체했습니다. rerun 뒤에는
버튼 활성 조건이 이전 inquiry 객체를 보는 문제도 있었습니다.

## 2. 실패가 발생한 정확한 계층

실패 지점은 `Dashboard UI -> DpsLookupOrchestrator` 뒤,
`AnswerService -> DpsEnrichmentService.policy`의 답변용 자동조회 정책이었습니다.
명시적인 운영자 조회와 답변 생성 중 자동 보강 조회가 같은 정책을 공유한 것이 원인입니다.

## 3. Dashboard 버튼 호출 경로

수정된 경로는 다음과 같습니다.

```text
Dashboard DPS button
-> selected inquiry 고정
-> DB의 order snapshot 재조회
-> ordinary order_id / actual order_date 검증
-> DpsLookupOrchestrator(explicit lookup)
-> DpsEnrichmentService
-> DPS Agent /lookup
-> Chrome 구매요청리스트 자동화
-> 결과 정규화
-> dps_lookup_results 저장
-> repository 재조회
-> inquiry별 session_state 갱신
-> Streamlit rerun
-> 같은 inquiry의 DPS 카드 렌더링
```

UI는 Agent client를 직접 호출하지 않습니다.

## 4. 전달된 번호 종류

- inquiry ID: 내부 선택 및 로그 연계에만 사용
- Naver 일반 `order_id`: DPS 검색 번호로 사용
- `product_order_id`: 저장은 하되 DPS 호출 전 차단
- DPS lookup order number: 검증된 일반 `order_id`와 동일
- 로그: 전체 번호 대신 마스킹된 번호만 기록

일반 주문번호가 없거나 상품주문번호만 있으면
`DPS 조회에는 일반 네이버 주문번호가 필요합니다. 먼저 주문 조회를 실행해 주세요.`
안내 후 호출하지 않습니다.

## 5. order_date 전달 여부

Naver 주문 조회에서 받은 실제 주문일을 `inquiries.order_date`에 저장하고 Agent 요청에
전달합니다. 문의 접수일 fallback은 제거했습니다. 실제 주문일이 없으면 DPS 호출을
차단하고 주문 조회를 먼저 실행하도록 안내합니다.

## 6. Agent 요청 여부

실제 Windows Agent를 자동 연결하고 DPS 로그인을 확인한 뒤 Dashboard 버튼 경로로
여러 요청을 전송했습니다. Activity Log에서 `DPS_LOOKUP_STARTED`와
`DPS_AGENT_CONNECTED`가 확인됐습니다.

## 7. Chrome 자동화 실행 여부

실제 Agent mode `WINDOWS_UI_AUTOMATION_TAB_V6_LOGIN_NAV`에서 Chrome DPS 탭에 연결해
구매요청리스트 검색과 상세조회가 실행됐습니다. 테스트 mock이 아니라 실제 Agent와
Chrome 자동화 응답을 사용했습니다.

## 8. 실제 조회 소요시간

실제 Dashboard 버튼 검증에서 확인된 응답시간은 다음과 같습니다.

- 55.104초
- 48.151초
- 55.741초
- 54.276초
- 43.132초
- 52.914초
- 54.322초

모든 값은 고객정보와 주문번호를 제외한 DB trace 기준입니다.

## 9. DB 저장 결과

Migration 7에서 inquiry order snapshot과 DPS trace 필드를 추가했습니다.

- inquiry: `order_date`, `order_status`, `order_lookup_at`
- DPS result: `correlation_id`, `lookup_started_at`, `lookup_completed_at`,
  `duration_seconds`, `cached`

대표 실제 검증 건은 `lookup_status=SUCCESS`, `error_code=LOOKUP_COMPLETE`,
`duration_seconds=55.104`, `cached=0`으로 저장됐으며 correlation ID도 존재했습니다.
저장 후 repository 재조회 결과를 카드에 사용합니다.

## 10. UI 갱신 결과

실제 Dashboard AppTest 버튼 이벤트가 Agent와 Chrome을 호출한 뒤 rerun됐고,
UI 예외 0건, 선택 inquiry 유지, inquiry별 `dps_result` 유지,
DPS 카드 `조회 성공` 전환을 확인했습니다. 결과는 전역 한 개가 아니라 inquiry ID별
map으로 보관합니다.

## 11. 결과 존재 주문 검증

결과가 존재하는 실제 주문은 최소 요구 수를 초과해 검증했습니다. 일반 주문번호와 실제
주문일 전달, Agent 응답, 판매/배송 정보 저장, DB 재조회, UI 카드 전환이 확인됐습니다.
일부 건은 판매번호가 없는 상세 부분 결과였으며 정상 결과와 별도로 보존됩니다.

## 12. 결과 없는 주문 검증

확보 가능한 실제 Naver 주문 후보를 추가 조회했지만 모두 DPS 결과가 존재했습니다.
따라서 실제 `NOT_FOUND` 주문의 Chrome 검증은 완료했다고 주장하지 않습니다.
대신 Agent의 정상 `NOT_FOUND` 응답을 `조회 성공 / 결과 없음`으로 저장·표시하는 경로는
자동 테스트로 검증했습니다. 실제 결과 없는 주문번호가 확보되면 같은 Dashboard 버튼
경로로 마지막 실데이터 확인이 필요합니다.

## 13. timeout 설정

- connect timeout: 7초
- response/read timeout: 100초
- 전체/poll timeout: 120초

실제 43.132~55.741초 조회가 정상 완료되어 45초 회귀가 없음을 확인했습니다.
연결 실패는 Agent offline, 응답 대기는 timeout으로 별도 분류합니다.

## 14. 추가 테스트

`tests/test_phase8_3_dashboard_dps_runtime.py`에 다음 회귀 테스트를 추가했습니다.

- Dashboard 명시 조회가 공통 orchestrator와 Agent를 호출
- 일반 order ID 및 실제 order date/request ID 전달
- product-order ID와 누락 order date 차단
- 주문 조회 결과 DB snapshot 저장
- timeout/offline 분류
- not-found 카드 상태
- 성공 trace 저장과 repository 재조회
- inquiry별 cache/session 분리
- 실제 Streamlit 버튼 코드 경로와 선택 유지
- 중복 클릭 방지
- 필수 Activity Log 이벤트

## 15. 전체 테스트 결과

Fake GPT 환경으로 네트워크 호출을 차단한 전체 회귀 테스트 결과:

```text
695 passed in 44.79s
```

## 16. 실제 Windows 검증 증거

대표 실제 inquiry의 Activity Log 순서:

```text
DPS_LOOKUP_REQUESTED
DPS_LOOKUP_STARTED
DPS_AGENT_CONNECTED
DPS_RESULT_SAVED
DPS_LOOKUP_SUCCEEDED
DPS_UI_REFRESHED
```

동일 건의 DB에는 성공 상태, 실제 duration, uncached 표시, correlation ID가 저장됐습니다.
API Key, Client Secret, OTP, 고객명, 주문번호 원문은 기록하거나 보고하지 않았습니다.

## 17. Phase 9 미수행 확인

이번 변경은 Phase 8.3 Dashboard DPS runtime 경로, 상태 보존, 오류 분류, 진단 로그와
테스트에만 한정했습니다. Phase 9 GPT-First 구조는 시작하지 않았습니다. 실제 Naver
답변 등록 잠금도 변경하지 않았습니다.
