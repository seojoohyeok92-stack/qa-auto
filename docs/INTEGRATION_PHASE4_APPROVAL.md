# Q&A auto 통합 4단계: 직원 검토 및 승인

## 목적

첨부된 `Q&A auto UI.png`, `Q&A auto UI 2.png`를 공식 설계 원본으로 삼아
기존 Streamlit 대시보드를 검토 작업 중심으로 재배치했다. Program Answer를
직원이 수정하고 승인하면 변경 불가능한 Final Answer 스냅샷을 만드는
구조다. 실제 네이버 등록은 아직 수행하지 않는다.

## 작업 전 분석

- 기준선 전체 테스트: `318 passed`
- 기존 `answer_drafts`에는 `original_answer`, `edited_answer`,
  `final_answer`, `review_status`, `posted`가 이미 있어 답변 본문용 테이블을
  새로 만들 필요가 없었다.
- 기존 workflow의 `READY_TO_POST`는 승인 후 등록 준비 상태로 재사용했다.
- DB version 1의 `workflow_status` CHECK를 파괴적으로 바꾸지 않기 위해
  업무상 `APPROVED`는 신규 `inquiries.approval_status`에 기록한다.
- 활동 로그는 `details_json`에 사용자, 동작, 상태를 저장할 수 있어 기존
  테이블을 유지했다.
- 기존 Dashboard는 6개 비클릭 KPI, 목록 아래 inline 상세 구조였다.

## DB migration v3

기존 migration 1·2는 수정하지 않았다. v3는 기존 데이터에 다음을 추가한다.

```text
inquiries
  approval_status  PENDING | APPROVED | POSTED
  approved_at
  approved_by

approval_history
  id, inquiry_id, answer_draft_id
  action           EDIT_SAVED | APPROVED | APPROVAL_CANCELLED | RESET
  actor, reason, previous_status, new_status, created_at
```

문의 삭제 시 승인 이력은 cascade되고, 답변 초안 참조는 초안 삭제 시
`SET NULL`이다. migration은 한 트랜잭션으로 실행되고 재실행되지 않는다.

## 승인 계층

```text
repositories/approval_repository.py
  ├─ 승인 상태 및 KPI 상태 조회
  ├─ 승인 이력 기록
  ├─ Final Answer 스냅샷과 승인 상태 원자적 저장
  └─ 승인 취소와 잠금 해제

services/approval_service.py
  ├─ 직원 수정/자동 저장
  ├─ 초기화
  ├─ 승인 및 승인 취소
  ├─ posted·approved 보호
  └─ workflow/activity log 갱신
```

답변 생성 버튼을 여러 번 눌러도 기존 `answer_drafts` 이력 정책대로 새
초안을 추가하며 이전 초안은 삭제하지 않는다. 승인은 최신 선택 초안의
`edited_answer`를 우선하고, 없으면 `original_answer`를 Final Answer로
복사한다.

## 상태 정책

```text
Program Answer 생성       workflow_status = REVIEW_PENDING
직원 수정/자동 저장       approval_status = PENDING
승인                      approval_status = APPROVED
                          workflow_status = READY_TO_POST
                          STAFF_REVIEW = COMPLETED
승인 취소                 approval_status = PENDING
                          workflow_status = REVIEW_PENDING
                          STAFF_REVIEW = RUNNING
실제 등록(후속 단계)      post_status/approval_status = POSTED
```

승인 후에는 직원 수정본을 잠근다. 실제 등록 전에는 승인 취소가 가능하나
사유가 필수다. posted 문의는 수정, 승인 취소, 초안 재생성, DPS 재조회,
삭제가 repository/service 계층에서 차단된다.

## 자동 저장

직원 수정 textarea는 Streamlit `on_change`에서 `ApprovalService`를 호출한다.
저장은 현재 DB 경로를 명시적으로 다시 열어 수행하며 다음 rerun에도 유지된다.
명시적 `직원 수정 저장` 버튼도 같은 서비스 경로를 사용한다. 답변 전문은
활동 로그에 넣지 않고 actor, action, status, draft id만 기록한다.

## 공식 UI 반영

- 상단 KPI: 신규 문의, 답변 초안 완료, 검토 대기, 등록 완료, 오류/주의
- KPI 카드: 클릭하면 페이지 이동 없이 문의 리스트만 필터
- 사이드바: Dashboard, 문의 관리, 진행 관리, DPS 관리, 설정, 활동 로그
- 중앙: 선택 강조 문의 리스트와 3단 답변 작업 영역
- 우측: 문의 상세, DPS 상세, 클릭 가능한 9단계 진행 영역
- Activity Log: 단계 클릭 시 modal 표시
- 디자인: dark theme, 청록 포인트, rounded card, compact premium SaaS

문의 상세는 문의 ID/시간/유형/일반 주문번호/상품/문의/기존 답변/고객 정보를
보인다. DPS 카드는 조회 상태, 판매번호, 배송·설치 상태, 설치예정일·유형,
최근 조회, 캐시, 조회시간·소요시간과 조회/재조회 버튼을 제공한다.

## 진행 단계

UI는 다음 9개 업무 단계를 표시한다.

1. 문의수신
2. DPS조회
3. 답변생성
4. 직원검토
5. 승인완료
6. 네이버등록
7. 학습후보
8. 학습반영
9. 완료

기존 DB의 9개 기술 step은 그대로 유지하고, 승인완료·학습후보·완료는
승인 상태, learning candidate 존재, posted 상태를 합성해 표시한다.

## Activity Log

modal에는 시간, 사용자, 동작, 상태, 메시지, 오류 열이 표시된다.
전화번호, 이메일, 주문번호, 토큰 등은 기존 `LogRepository`의 마스킹
정책을 거쳐 저장되며 답변 전문과 고객 상세는 기록하지 않는다.

## 테스트

```powershell
Set-Location -LiteralPath "C:\path\to\qa-auto"
python -m pytest -ra --basetemp ".pytest_tmp_manual"
python -m compileall .
```

추가 테스트는 migration v3, 직원 수정·자동 저장, 승인 Final Answer 스냅샷,
직접 승인, 승인 취소 사유, 승인 취소 후 수정, 초기화, posted 편집·취소·삭제
보호, 승인 이력, KPI 분류/조회 범위를 검증한다.

## 아직 구현하지 않은 기능

- 네이버 실제 답변 등록과 등록 버튼
- OpenAI 실제 호출
- 학습 후보 자동 생성 및 학습 자동 반영
- OTP/로그인 변경
- 별도 진행 관리·DPS 관리·설정 전면 화면

승인 완료는 “등록 가능 상태”일 뿐 등록 성공이 아니다.

## 다음 단계

실제 등록은 `ApprovalRepository.get_inquiry_approval()`이 `APPROVED`이고
최신 draft의 `final_answer`가 있으며 posted가 아닌 경우에만 허용해야 한다.
등록 성공 트랜잭션은 `answer_drafts.posted/posted_at`,
`inquiries.post_status/approval_status/workflow_status`, `NAVER_POST`
workflow step 및 activity log를 함께 갱신해야 한다. 실패하면 승인과 Final
Answer는 보존하고 등록 상태만 재시도 가능하게 기록한다.
