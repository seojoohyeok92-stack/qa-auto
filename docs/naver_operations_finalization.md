# Q&A Auto 네이버 운영 최종 정리

## 요약

Dashboard 5개 운영 카드를 KST 기준의 SQL `COUNT(DISTINCT ...)` 집계로 정리하고, FLOW와 STOCK 의미를 분리했다. 자동 Positive Learning 생성 경로를 모두 차단했으며, Learning 반영은 관리자의 명시적 Positive/Negative/제외 판단으로만 수행된다. 기존 `NaverPostService`를 재사용하여 관리자 모드와 무관한 수동 네이버 답변 등록 UI를 연결하고, 현재 Final Answer에 대해 Validator·DPS·주문·Missing Item 등 기존 안전조건을 다시 검사하도록 했다.

전체 검증 결과는 `3641 passed, 0 failed, 0 skipped`이며 schema/migration 및 실제 외부 API 호출은 없다.

## 시작 상태

- 시작 branch: `main`
- 시작 HEAD: `2b9b1b8ef0b90f9021ffd024357534e7da1b3a89`
- 시작 working tree: clean
- 개발 PC에서만 작업했고 서버 PC와 운영 외부 API는 사용하지 않았다.

## 5개 카드 기존 집계 구조

기존 카드는 Dashboard 필터 결과와 work item의 `registered_at`을 Python에서 조합했다. 이 방식은 host timezone에 의존했고, 검토 대기와 오류/주의가 현재 상태가 아닌 과거/표시 목록 의미와 섞였으며, draft 존재 조건 때문에 실제 현재 조치 대상이 누락될 수 있었다.

## 5개 카드 실제 DB 검증

2026-08-31 KST 기준 개발 DB를 SQLite read-only로 대조했다. 최신 수집 데이터는 2026-08-27이므로 오늘 FLOW는 모두 0건이다.

| 카드 | 기존 표시 집계 | 수정 후 집계 | 의미 |
|---|---:|---:|---|
| 신규 문의 | 2,387 | 2,772 | 전체 unique inquiry, 오늘은 DB 최초 insert 기준 0 |
| 답변 초안 완료 | 346 | 362 | non-empty usable answer가 있는 unique inquiry, 오늘 0 |
| 검토 대기 | 346 | 363 | 현재 승인 대기·미등록·`REVIEW_PENDING/NEEDS_ATTENTION` STOCK |
| 승인 완료 | 7 | 7 | 현재 명시적 승인 완료 unique inquiry, 오늘 0 |
| 오류/주의 | 112 | 110 | 현재 미해결 `NEEDS_ATTENTION/FAILED/POST_FAILED/POST_UNKNOWN` STOCK |

## 신규 문의 집계

`inquiries.created_at`을 DB 최초 insert 시각으로 사용한다. `COUNT(DISTINCT id)`이므로 재동기화 update는 신규로 중복 집계되지 않는다. 전체는 2,772건, 오늘은 0건이다.

## 답변 초안 완료 집계

`answer_drafts.final_answer`, `edited_answer`, `original_answer` 우선순위로 non-empty answer를 검사하고 `COUNT(DISTINCT inquiry_id)`로 집계한다. 재생성 횟수와 무관하게 문의당 1건이다. 전체 362건, 오늘 0건이다.

## 검토 대기 집계

`approval_status='PENDING'`, 미등록 상태, `workflow_status IN ('REVIEW_PENDING','NEEDS_ATTENTION')`인 현재 STOCK만 포함한다. 과거에 검토 대기였더라도 승인·등록으로 해소된 문의는 제외한다. 현재 363건이다.

## 승인 완료 집계

`approval_status='APPROVED'`인 명시적 직원 승인만 집계한다. Auto Post 성공과 승인을 혼합하지 않는다. 전체 7건, 오늘 0건이다.

## 오류/주의 집계

현재 승인 대기·미등록이면서 `NEEDS_ATTENTION`, `FAILED`, `POST_FAILED`, `POST_UNKNOWN`인 unique inquiry만 포함한다. 해결된 과거 오류 이력은 삭제하지 않되 메인 STOCK 카드에서는 제외한다. 현재 110건이다.

## KST 날짜 처리

`datetime.now(ZoneInfo('Asia/Seoul')).date()`로 기준일을 만들고, UTC ISO timestamp는 SQL `date(timestamp, '+9 hours')`로 KST calendar day와 비교한다. 서버 timezone에는 의존하지 않는다.

## 자동 Positive Learning 기존 구조

기존 자동 생성 후보는 7개였다.

1. 관찰기간 경과 후 unchanged Auto Post 승격
2. Naver 직접 수정 감지 후 `AUTO_POST_CORRECTED`
3. 동기화된 과거 seller answer의 `SELLER_ANSWER`
4. post review의 no-change 완료
5. Naver correction 완료 후 Learning 저장
6. 내부 posted answer 수정 저장 시 Positive 생성
7. 반복 확인 structured signal의 환경변수 기반 자동 승격

## 자동 Learning 제거

- `PositiveLearningService.observe()`는 호환 facade로 남지만 항상 `MANUAL_APPROVAL_REQUIRED`를 반환한다.
- Naver sync는 직접 수정과 seller answer를 품질/관찰 이력으로만 기록하고 `learning_examples`를 만들지 않는다.
- post review, Naver correction, 내부 직원 수정은 이력/feedback만 기록하며 Positive를 자동 생성하지 않는다.
- `AUTO_VERIFIED_FACT_PROMOTION_ENABLED=true`가 설정돼도 자동 승격은 항상 꺼진다.
- 자동 생성용 `LearningService` 메서드는 과거 데이터/호환성 때문에 보존하지만 production runtime의 자동 caller는 0개다.
- 기존 Learning row는 삭제하거나 비활성화하지 않았다.

## 수동 Positive/Negative Learning

- 명시적 `ApprovalService.approve`, `approve_posted_answer`, `approve_posted_staff_correction`은 기존 Human Verified Positive 경로를 유지한다.
- 명시적 Negative/제외/revoke는 기존 `learning_feedback` 경로를 그대로 유지한다.
- 기존 수정 피드백 UI·로직·저장 구조는 변경하지 않았다.

## Naver 직접 수정과 Learning 분리

Naver sync가 직접 수정을 감지하면 `answer_versions.version_kind='NAVER_CORRECTION_APPLIED'` 이력으로 남는다. `LearningPerformanceRepository`는 이 이력을 직원 수정률에 반영한다. 동일 inquiry는 SQL 집계에서 중복되지 않는다. 해당 감지는 Positive/Negative Learning을 자동 생성하지 않는다.

## 기존 Learning 데이터 보존

기존 `learning_examples`, `learning_feedback`, `historical_cases`는 수정·삭제·일괄 비활성화하지 않았다. 기존 historical retrieval과 Negative/revoke 회귀 테스트가 모두 통과했다.

## 수동 Naver 등록 기존 구조

새 POST 구현을 만들지 않고 기존 `NaverPostDryRunService`, `NaverPostService`, `NaverPostRepository`를 재사용했다. 기존 transaction 기반 acquire, idempotency, `POSTING/POSTED/POST_UNKNOWN` guard를 그대로 사용한다.

## 수동 등록 UI

문의 상세에 `네이버 답변 등록` 패널을 관리자 모드 조건 밖에 배치했다. `승인`은 Final Answer/Human approval만 수행하고 실제 등록은 별도 버튼과 확인 단계에서 수행한다.

## 등록 가능 조건

버튼 클릭 시 현재 active/latest draft의 `final_answer`로 preflight한다. `NaverPostService.post()`도 전송 직전에 같은 dry run을 다시 수행한다. 수동 등록은 자동등록 대상 intent가 아니었다는 사유만 제한적으로 허용하고 다음 blocker는 우회하지 않는다.

- empty/invalid Final Answer
- Validator failure
- Missing Item 또는 직원 검토 route
- order ID 필요
- DPS 필요/미충족
- Product Facts 및 현재성 안전 blocker
- Naver target/source identifier 오류
- 이미 등록·처리 중·결과 불명 상태

과거 `REVIEW_REQUIRED` 이력 자체는 영구 차단 사유가 아니며, 현재 안전한 Final Answer와 상태가 조건을 충족하면 등록할 수 있다.

## Validator/DPS Safety

`AutoPostTechnicalValidator`와 `AutoProcessingEligibilityService`를 수동 preflight에도 적용했다. 기존 Auto Post와 같은 Validator, Missing Item, order, DPS, Product Facts blocker를 유지한다. 테스트에서 Validator/DPS 우회는 각각 0건이다.

## POST 성공 처리

현재 UI Final Answer가 `NaverPostPayloadBuilder` payload에 그대로 사용된다. 실제 Naver 성공 응답 후에만 repository가 `POSTED` 및 완료 workflow를 기록한다. 기존 Positive가 있으면 posted provenance만 갱신할 수 있지만 새 Learning row는 생성하지 않는다.

## POST 실패 처리

실패/불명 응답은 답변완료로 처리하지 않는다. Final Answer와 승인 상태는 보존하며, `POST_FAILED`는 명시적 retry, `POST_UNKNOWN`은 원격 상태 확인을 요구한다.

## 중복 POST 방지

UI의 등록 완료/진행/불명 상태 비활성화에 더해 service의 `source_answered` guard와 repository transaction acquire를 사용한다. double click 및 Auto/manual 경쟁 테스트에서 중복 POST는 0건이다.

## Kakao 연계

Kakao outbox, retry, dedup, 성공/실패 routing은 변경하지 않았다. 실제 POST 성공만 답변 내용을 표시하고 미등록/실패는 `답변: -`로 표시하는 기존 계약의 regression은 0건이다.

## 직원 수정률 유지

내부 수정과 Naver 직접 수정은 품질 측정 신호로 유지한다. `answer_versions`와 `post_reviews`를 사용하며 auto-created `learning_examples`에 의존하지 않는다. 내부+Naver 경로가 함께 존재해도 inquiry 단위로 1건이다.

## 기존 8개 실패 분류

| 실패 테스트 | 분류 | 근거 및 처리 |
|---|---|---|
| `test_full_pipeline_from_two_staff_edits_to_next_inquiry_answer_generation` | OUTDATED_TEST | 반복 확인만으로 structured signal이 자동 승격된다고 기대했다. 명시적 Positive 2건 생성과 자동 signal 승격 0건을 검증하도록 변경했다. |
| `test_streamlit_post_prepare_panel_keeps_actual_button_locked` | OUTDATED_TEST | 버튼명이 과거 `네이버 실제 등록`이었다. 새 운영 문구 `네이버 답변 등록`으로 갱신하되 locked assertion은 유지했다. |
| `test_posted_answer_staff_correction_has_precise_learning_provenance` | OUTDATED_TEST | 내부 직원 수정이 Positive도 자동 생성한다고 기대했다. 명시적 Negative feedback/provenance는 유지하고 자동 Positive 0건을 검증한다. |
| `test_unedited_naver_answer_approval_promotes_only_posted_truth` | OUTDATED_TEST | sync 시 seller answer가 먼저 자동 Learning이 된다고 기대했다. 승인 전 0건, 명시적 승인 후 Human Verified Positive 1건을 검증한다. |
| `test_human_verified_authority_wins_concurrent_source_answered_upsert` | OUTDATED_TEST_SETUP | runtime sync가 legacy automatic row를 만든다는 setup 가정이 폐기됐다. legacy row를 fixture에서 명시적으로 seed하여 repository 권한/동시성 계약 자체는 유지했다. |
| `test_general_dashboard_does_not_render_naver_post_prepare_card` | OUTDATED_TEST | 관리자 외 Dashboard에는 등록 UI가 없어야 한다는 과거 계약이었다. 관리자 모드와 무관하게 표시되는 새 요구를 검증한다. |
| `test_validator_is_compact_and_vertical_stage_chain_removed` | OUTDATED_TEST | answer panel 내부에 등록 잠금 문구가 있다고 기대했다. 승인/등록 분리 문구를 검증하고 등록 패널이 별도임을 확인한다. |
| `test_synced_seller_answer_is_style_only_not_human_verified` | OUTDATED_TEST | seller answer sync가 style-only Positive를 자동 생성한다고 기대했다. 관찰 로그는 남고 Learning은 0건임을 검증한다. |

추가 production 수정은 Learning을 저장하지 않으면서 “저장했습니다”라고 하던 sync 로그 문구를 “품질 측정 이력으로 기록했습니다”로 바로잡은 1건이다. 수정 피드백 구조에는 손대지 않았다.

## Focused Tests

- 8개 실패 파일 1차: 61 passed, 1 test-fixture assertion 실패
- 해당 fixture 교정 단건: 1 passed
- 정책/수동등록/Dashboard/Negative/Historical 통합 focused: `161 passed, 0 failed`

## 전체 Tests

- `python -m pytest -q`
- `3641 passed, 0 failed, 0 skipped`
- 소요시간: 1182.67초(19분 42초)

## 잔여 한계

- 기존 자동 Learning 데이터와 자동 생성용 호환 메서드는 이력 조회와 과거 데이터 호환을 위해 남아 있다. production 자동 caller는 없다.
- 개발 DB에 2026-08-31 당일 수집 데이터가 없어 오늘 FLOW는 모두 0건이다. KST 경계 및 중복 방지는 temporary DB fixture로 검증했다.

## 최종 판정

CRITICAL 0, HIGH 0, MEDIUM 0, LOW 0이다. schema/migration 변경과 외부 실제 API 호출은 모두 0이다.

**NAVER OPERATIONS FINAL READY**

— Dashboard 운영 집계 · 수동 Learning · 수동 답변등록 최종 정리 완료
