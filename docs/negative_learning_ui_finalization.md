# Negative Learning 관리/UI 최종 정리

- 작업일: 2026-08-30~2026-08-31 (Asia/Seoul)
- 시작 branch: `main`
- 시작 HEAD: `c531dc1eaec42fae0f133122ba5a008f37e4812f`
- 작업 환경: 개발 PC repository만 사용
- 금지 사항 준수: 운영 DB, Naver, DPS, Kakao, 서버 PC, `.env`, migration을 변경하거나 호출하지 않음

## [요약]

기존 `learning_feedback`, `learning_signals`, `learning_signal_confirmations`, `answer_feedback_signal_provenance` 구조를 그대로 사용해 Dashboard Negative 평가 취소를 `UI → service → repository` 계층으로 연결했다. Negative 취소는 physical DELETE가 아니라 평가 row와 해당 confirmation의 soft revoke이며, 과거 signal/provenance row는 보존한다.

Learning Manager에는 Positive, Negative, 학습 제외를 각각 녹색·적색·중립색 badge로 구분했고, feedback 상세에서 실제 `correction_reason`, `correction_note`, revoke 사유와 처리자를 확인할 수 있게 했다. 새 schema, migration, 별도 Negative 저장소는 만들지 않았다.

## [시작 상태]

- repository root: `C:/Users/user/Desktop/프로젝트/Q&A 통합/git/qa-auto`
- branch: `main`
- HEAD: `c531dc1eaec42fae0f133122ba5a008f37e4812f`
- 시작 working tree: `docs/learning_storage_architecture_final_audit.md` 1개가 untracked였으며 그대로 보존
- staged 변경: 없음
- unrelated 사용자 변경: 없음

## [기존 Negative Learning 구조]

- Negative/Intent correction/Excluded source-of-truth: `learning_feedback`
- 구조화 signal: `learning_signals`
- signal의 독립 근거와 revoke 상태: `learning_signal_confirmations`
- 답변 생성 시 signal 부착/사용 추적: `answer_feedback_signal_provenance`
- Negative 생성: `LearningFeedbackService.capture_dashboard_negative` → `LearningFeedbackRepository.save_dashboard_evaluation_atomic`
- signal 변환: `LearningFeedbackService._capture_signal` → `LearningSignalService.capture` 또는 `auto_extract_and_capture`
- runtime: `LearningContextService.build` → `LearningSignalService.retrieve` → `LearningSignalRepository.candidates`

`learning_candidates`, `outputs/learning`, `configuration.xlsx`는 사용하지 않았다.

## [기존 UI 상태]

작업 전 정적 조사 결과는 다음과 같았다.

| 대상 | 시작 판정 | 근거 |
|---|---|---|
| Dashboard Positive 평가 | `IMPLEMENTED_AND_CONNECTED` | `ApprovalService.approve` → `learning_examples` |
| Dashboard Negative 평가 | `IMPLEMENTED_AND_CONNECTED` | reason/memo와 선택적 `BAD_PATTERN`/`CORRECTION` 입력 포함 |
| 학습 제외 저장·취소 | `IMPLEMENTED_AND_CONNECTED` | `capture_dashboard_excluded` / `revoke_dashboard_excluded` |
| Learning Manager 상태 조회 | `IMPLEMENTED_AND_CONNECTED` | Positive와 feedback section 분리, active/revoked label 존재 |
| 세 상태 공통 시각 badge | `PARTIALLY_CONNECTED` | 텍스트/section은 있으나 공통 색상 badge가 없음 |
| Negative repository revoke | `IMPLEMENTED_BUT_NOT_EXPOSED` | `deactivate_dashboard_evaluation` 존재, 호출자 없음 |
| Negative service/UI revoke | `NOT_IMPLEMENTED` | service method와 버튼 없음 |
| revoke 후 runtime 배제 | `PARTIALLY_CONNECTED` | confirmation은 비활성화하지만 manual signal과 auto `BAD_PATTERN`이 남을 수 있음 |
| provenance | `IMPLEMENTED_AND_CONNECTED` | soft revoke와 독립된 추적 table |

## [구현한 변경]

### Backend

1. `LearningFeedbackRepository.deactivate_dashboard_evaluation`
   - 같은 answer identity의 `NEGATIVE`와 `INTENT_CORRECTION` row를 한 transaction에서 soft revoke한다.
   - `metadata_json.status=REVOKED`, `revoke_reason`, `revoked_by`, `revoked_at`을 기록한다.
   - 영향받은 feedback에 직접 연결된 active confirmation만 비활성화한다.
   - signal과 과거 provenance는 삭제하지 않는다.
2. `LearningFeedbackService.revoke_dashboard_negative`
   - reason, `AnswerProvenance`, 활성 평가 존재 여부를 검증한다.
   - repository revoke 결과가 예상 row 수와 다르면 성공으로 표시하지 않는다.
   - 이미 취소된 평가의 재취소는 명확한 `ValueError`로 종료되며 DB 상태는 변하지 않는다.
3. `LearningSignalRepository.candidates`
   - manual signal은 연결된 feedback/example/historical source가 현재 active일 때만 runtime 후보가 된다.
4. `LearningSignalService._is_eligible`
   - auto `GOOD_PATTERN`/`BAD_PATTERN`은 최소 1개의 live confirmation이 있을 때만 사용한다.
   - 기존 즉시 활성 의미는 유지하되 마지막 source revoke 후에는 제외된다.

### UI

1. Dashboard에서 현재 선택한 answer identity에 활성 Negative가 있을 때만 `Negative 평가 취소`를 표시한다.
2. 취소 사유와 단일 checkbox 확인이 모두 있어야 버튼이 활성화된다.
3. 취소 후 카드에 `Negative 평가 취소됨`, 기존 Negative 이유/메모, 취소 사유·처리자, `REVOKED` 상태를 표시한다.
4. Learning Manager 상단에 Positive/Negative/학습 제외 색상 badge를 추가했다.
5. Learning Manager feedback 상세에 실제 reason code의 사용자용 label, memo, revoke 사유·처리자를 표시한다.

## [Positive/Negative/학습 제외 UI]

- Positive: 녹색 계열 badge. 기존 `learning_examples` section, 승인/자동/비활성 label은 그대로 유지했다.
- Negative: 적색 경고 계열 badge. active는 `Negative`, revoke 후 `Negative 취소`/`Negative 평가 취소됨`으로 표시한다.
- 학습 제외: 회색·중립 계열 badge. `EXCLUDED`는 Negative와 별도 section/filter/label 의미를 유지한다.
- Dashboard의 기존 Negative card와 Excluded card도 서로 다른 색상 계약을 유지한다.

검증한 표시 사례 수:

- Positive 표시: 3
- Negative 표시: 4
- 학습 제외 표시: 3

## [Negative Reason]

새 필드를 만들지 않고 기존 값을 사용한다.

- 분류: `learning_feedback.correction_reason`
- 메모: `learning_feedback.correction_note`
- routing 교정: `corrected_intent`
- revoke 정보: `metadata_json.revoke_reason`, `revoked_by`, `revoked_at`, `status`

`CorrectionReason`과 `ExclusionReason`의 기존 label map을 Learning Manager에서도 재사용한다. Negative reason 표시 3개 사례를 검증했다: active Dashboard card, revoke 후 card, Manager label+memo formatter.

## [Revoke 구조]

```text
Dashboard의 활성 Negative
  ↓ 취소 사유 + 확인 checkbox
LearningFeedbackService.revoke_dashboard_negative
  ↓ identity/active/reason 검증
LearningFeedbackRepository.deactivate_dashboard_evaluation
  ├─ learning_feedback.active = 0
  ├─ metadata_json에 revoke 감사정보 기록
  └─ 해당 learning_feedback의 confirmation.active = 0
       ↓
LearningSignalRepository / LearningSignalService runtime gate
       ↓
새 retrieval에서 revoked Negative signal 제외

보존:
  learning_feedback row
  learning_signals row
  answer_feedback_signal_provenance 과거 row
```

revoke 검증은 service, repository 상태, UI confirmation/action, linked confirmation, 연속 호출 idempotency의 5개 관점으로 수행했다.

## [Runtime Negative]

- Scenario A: 관련 설치일 문의에서 active `BAD_PATTERN`이 정상 조회됨.
- Scenario B: 같은 상품이라도 리모컨 topic 문의에는 설치일 `BAD_PATTERN`이 조회되지 않음.
- Scenario C: Negative revoke 뒤 같은 설치일 문의에서 해당 signal이 조회되지 않음.
- Scenario D: revoke 전 기록한 provenance가 revoke 뒤에도 남음.
- Scenario E: revoke 뒤 동일 answer를 Positive 승인하고 `learning_examples` retrieval이 정상 동작함.
- Scenario F: 기존 Historical 후보/retrieval 관련 전체 regression이 정상 동작함.

revoked signal runtime 사용 오류: 0.

## [Provenance]

revoke는 `answer_feedback_signal_provenance`를 UPDATE/DELETE하지 않는다. focused test에서 revoke 전 provenance 1건을 만들고 revoke 후에도 동일 1건이 남는 것을 확인했다. 현재 runtime 권위 제거와 과거 사용 기록 보존을 분리했다.

provenance 손실: 0.

## [Cross-topic Safety]

기존 product identity/topic compatibility를 넓히지 않았다. source-active/live-confirmation gate만 추가했다. 설치일 Negative가 리모컨 문의에 붙지 않는 focused 시나리오와 기존 product/topic compatibility suite가 모두 통과했다.

cross-topic negative leakage: 0.

## [Positive Regression]

- `learning_examples` source-of-truth 변경 없음.
- 승인 저장, retrieval, validity, approval revoke, provenance UI 계약 변경 없음.
- revoked Negative 뒤 새 Positive 승인과 후보 조회 성공.
- 기존 Positive 관련 focused 및 전체 tests 성공.

Positive regression: 0.

## [Historical Regression]

- `historical_cases`, `historical_case_versions`, `promoted_learning_id` 코드 변경 없음.
- Historical active source를 manual signal gate에서 기존 `active` 계약대로 확인할 뿐 promotion/검색 규칙은 변경하지 않음.
- Historical focused suite 전체 성공.

Historical regression: 0.

## [UI 검증]

Streamlit `AppTest`와 pure helper test로 다음을 확인했다.

1. Positive badge와 승인 상태 표시
2. Negative badge와 active 상태
3. 학습 제외 badge와 active/revoked 상태
4. Negative reason label 및 memo
5. 활성 Negative에서만 revoke 입력/버튼 노출
6. 사유+checkbox 전에는 버튼 비활성
7. revoke 성공 후 취소 카드·사유 표시
8. 이미 revoke된 상태에서는 revoke 버튼 미노출

실제 운영 서버나 browser session은 사용하지 않았다.

## [테스트]

- 문법 검증: 수정 Python 파일 `py_compile` 성공
- diff 검증: `git diff --check` 성공
- focused 묶음: 94 passed / 0 failed
- 전체 suite: 3,593 passed / 0 failed / 0 skipped
- 전체 실행 시간: 1,153.15초(19분 13초)
- 테스트 DB: pytest `tmp_path` 기반 SQLite
- 외부 호출: mock/fixture 경로만 사용

## [잔여 문제]

이번 MEDIUM gap은 해결되어 MEDIUM backlog는 0이다. 기존 저장 구조 감사의 범위 밖 LOW 4건은 변경하지 않았다.

| 등급 | 수 | 내용 |
|---|---:|---|
| CRITICAL | 0 | 없음 |
| HIGH | 0 | 없음 |
| MEDIUM | 0 | Dashboard Negative revoke service/UI/runtime 연결 완료 |
| LOW | 4 | confirmation의 비-FK `approval_history_id`, Historical version 조회/복원 부재, Local SELLER_ANSWER 이중 후보 가능성, `learning_candidates` dead schema |

## [최종 판정]

완료 조건을 모두 충족했다.

- Positive/Negative/학습 제외 UI 구분 완료
- Negative reason 확인 가능
- revoke service/UI 연결 완료
- revoked Negative runtime 미사용
- cross-topic leakage 0
- Positive regression 0
- Historical regression 0
- provenance 손실 0
- 전체 tests failed 0
- CRITICAL 0

**NEGATIVE LEARNING UI READY**
**— Negative Learning 관리/UI 최종 정리 완료**
