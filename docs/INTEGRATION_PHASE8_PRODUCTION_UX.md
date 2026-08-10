# Integration Phase 8 완료 보고서

## 1. Dashboard 개선

- 기준 이미지 `Dashboard.png`의 다크 테마, 카드 계층, 좌측 메뉴,
  5개 KPI, 목록/상세 2단 레이아웃을 반영했다.
- 페이지 제목 32px, 섹션 제목 22px, 카드 제목 18px,
  본문/버튼 16px, 표 15px 이상을 최종 CSS 우선순위로 보장했다.
- KPI는 신규 문의, 답변 초안 완료, 검토 대기, 등록 완료,
  오류/주의를 표시하며 오늘/전체 건수를 함께 보여준다.
- 문의 목록은 상태, 문의유형, 상품명, 문의요약, 주문번호,
  문의일, 스토어를 한 행에 표시하고 긴 문장을 말줄임 처리한다.
- 선택 문의는 session state에 유지되며 상세, DPS 결과,
  답변 초안과 승인 상태는 DB 기반으로 재렌더링된다.

## 2. UI 변경 전후

- 변경 전: 개발자 진단 중심, 7~10px의 작은 보조 글씨,
  답변 생성 단계와 운영 상태가 여러 화면에 분산됐다.
- 변경 후: 운영자 가독성 기준을 적용하고 문의 목록/답변 검토와
  문의·DPS·진행 정보를 좌우 카드로 분리했다.
- 답변 흐름은 분석 → Program Answer → Staff Edit → Validator
  → 승인 → Final Answer 순서로 표시한다.

## 3. Dashboard DPS

- `DpsLookupOrchestrator`를 Dashboard의 공통 DPS 진입점으로 추가했다.
- 일반 `order_id`가 없거나 상품주문번호가 전달되면 Agent 호출 전에 차단한다.
- 조회 결과는 기존 `DpsEnrichmentService`와 `DpsRepository`를 통해
  통합 DB에 저장되고 Dashboard 재실행 시 즉시 표시된다.
- Windows 실제 강제 재조회 결과:
  - 일반 주문번호와 주문일 전달 확인
  - Agent 호출 및 DB 저장 성공
  - 조회 결과 `NOT_FOUND / NO_DPS_RESULT`
  - 캐시 미사용
  - Activity Log에 요청, 시작, 결과 이벤트 기록

## 4. 실제 GPT

- `.env`에서 `ACTIVE / openai / gpt-5.6-sol`을 선택했다.
- 모델은 `QNA_GPT_MODEL`과 허용 모델 목록으로 변경 가능하다.
- OpenAI Responses API와 기존 Governance/Provider Factory 구조를 유지했다.
- 명시적 `GPT 답변 생성` 버튼에서만 호출한다.
- GPT JSON 출력 계약과 허용 Fact 경로를 명시해 Validator 호환을 보장했다.
- 성공한 Windows 실제 호출:
  - Provider: OpenAI
  - Model: `gpt-5.6-sol`
  - 결과: Validator 통과, Rule Fallback 없음
  - 상태: `NEEDS_REVIEW`
  - 응답시간: 14,707ms
  - 입력/출력/전체 토큰: 2,949 / 745 / 3,694
  - 예상 비용: 53.78775원

예상 비용은 공식 USD 토큰 가격과
`QNA_GPT_USD_KRW_RATE`를 사용한 참고값이며 실제 청구액과 다를 수 있다.

## 5. Program Answer와 승인

- Program Answer 영역에 출처, 모델, 생성시각, 응답시간,
  입력/출력/전체 토큰, 예상 비용을 표시한다.
- 실제 검증에서 Staff Edit 저장, Approval, Final Answer 생성을 확인했다.
- 승인 후에도 `post_status=NOT_POSTED`를 확인했다.
- 네이버 실제 등록 기능은 추가하거나 활성화하지 않았다.

## 6. UAT 단순화

- 기본 화면을 네이버 연결, DPS 연결, GPT 연결, DB,
  등록 잠금의 5개 카드로 축약했다.
- 각 카드는 상태, 설명, 조치, 다시 확인 버튼을 표시한다.
- 기존 진단 표와 지표는 `개발자용 상세보기`로 이동했다.

## 7. Activity Log

- DPS 요청/시작/결과, GPT 요청/성공/검증,
  Staff Edit, 승인 이벤트 기록을 확인했다.
- API key와 Secret은 화면, 로그, 보고서에 출력하지 않았다.

## 8. 검증 결과

- 운영 환경 Validator: `NORMAL`
- GPT Governance 설정 오류: 0
- DB health: 정상
- Streamlit health: HTTP 200
- 집중 테스트: 123 passed
- 전체 테스트: 660 passed in 34.82s
- 테스트에서는 외부 호출과 비용 발생을 막기 위해 프로세스 환경에서만
  Fake Provider를 사용했고 운영 `.env`는 ACTIVE/OpenAI로 유지했다.

자동 브라우저 연결을 사용할 수 없어 스크린샷 기반 회귀 비교는
수행하지 못했다. Streamlit AppTest, 실제 서버 렌더링 시작,
health endpoint 및 UI 테스트로 예외 없는 렌더링을 검증했다.
