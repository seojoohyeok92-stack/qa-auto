# Learning 저장 구조 최종 감사

- 조사일: 2026-08-30 (Asia/Seoul)
- 조사 기준: `main` / `c531dc1eaec42fae0f133122ba5a008f37e4812f`
- 조사 방식: schema, repository, service, UI 연결, 정적 자료 및 필요한 Git 이력의 정적 확인
- 제외 사항: DB 데이터 변경·조회 실험, migration 실행, 테스트, production/test/config 수정, 운영 작업, commit/push

## [최종 요약]

현재 운영 Learning의 primary source-of-truth는 `OJE_AUTOMATION_DB_PATH`가 가리키는 SQLite이며, 미설정 시 기본 경로는 `data/oje_automation.db`이다. 경로 결정과 FK 활성화는 `repositories/database.py:get_database_path`, `Database._connect`에 있다.

본문과 책임은 다음처럼 분리되어 있다.

1. Positive Learning 본문과 현재 사용 가능 상태의 source-of-truth는 `learning_examples`이다.
2. Negative / Intent correction / Excluded 평가의 source-of-truth는 `learning_feedback`이다.
3. Structured Learning 본문은 `learning_signals`, 그 신호의 독립 확인·취소 상태는 `learning_signal_confirmations`가 담당한다.
4. 승인된 답변 본문의 현재본은 `answer_drafts.final_answer`, 승인 여부의 현재 상태는 `inquiries.approval_status/approved_at/approved_by`이다. `approval_history`는 append-only 성격의 승인 이벤트 이력이지 현재 상태의 source-of-truth가 아니다.
5. 고객에게 실제 게시되어 관측된 답변은 `naver_posted_answers`의 `is_current=1` 행이 별도 source-of-truth이다. 승인본과 게시 관측본은 같은 개념이 아니다.
6. Historical Answer 현재본의 source-of-truth는 `historical_cases`, 변경 시점 snapshot은 `historical_case_versions`이다.
7. `answer_learning_provenance`와 `answer_feedback_signal_provenance`는 답변 생성 context에 무엇이 붙고 실제 사용되었는지를 추적하는 provenance 전용 저장소이다.
8. `answer_data/learning/model_data_with_color.json`은 이름과 달리 운영 Learning 사례 저장소가 아니라 정적 모델 catalog이다.
9. `learning_candidates`는 초기 schema만 남고 production read/write가 없는 dead schema이다. `outputs/learning`과 `configuration.xlsx`는 현재 repository에 없으며 현 runtime Learning 경로가 아니다.

CRITICAL 0건, HIGH 0건이다. 따라서 남아 있는 LOW/MEDIUM 문서화·일관성 개선 항목은 구조 확정을 막지 않는다.

## [테이블별 Source of Truth]

| 테이블 | 저장 내용 / 역할 | 권위 분류 | PK | 주요 FK·연결 식별자 | 실제 write 경로 | runtime read 경로 |
|---|---|---|---|---|---|---|
| `learning_examples` | 마스킹된 질문, 정규화 질문, 승인/관측/승격 답변 snapshot, 품질·상품 scope·유효기간·활성 상태 | **Positive Learning source-of-truth** | `id` | `source_key UNIQUE`; `inquiry_id`, `answer_draft_id`, `approval_history_id`는 모두 `ON DELETE SET NULL`; Historical 승격은 `metadata_json.historical_case_id`와 역방향 `historical_cases.promoted_learning_id` | `services/learning_service.py:LearningService.capture_approved`, `capture_verified_posted_answer`, `capture_auto_post_version`, `capture_historical_promotion`; `repositories/learning_repository.py:LearningRepository.upsert`, `upsert_human_verified_atomic` | `services/learning_context_service.py:LearningContextService.build` → `services/similar_answer_service.py:SimilarAnswerService.context/search` → `LearningRepository.candidates` |
| `learning_feedback` | 직원 correction, Negative, Intent correction, Excluded, Historical review의 마스킹된 평가 snapshot과 상태 | **Negative/Excluded source-of-truth** | `id` | `source_key UNIQUE`; `inquiry_id`, `answer_draft_id`, `historical_case_id`는 `SET NULL`; `original_answer_source + original_answer_reference_id`는 논리 식별자이며 FK 아님 | `services/learning_feedback_service.py:LearningFeedbackService.capture_staff_correction`, `capture_dashboard_negative`, `capture_dashboard_excluded`, `capture_historical_review` → `repositories/learning_feedback_repository.py` | `LearningRepository.candidates`의 exact rejected-answer 차단, `LearningService.assert_positive_allowed`, Learning Manager/Review UI; 구조화된 내용은 아래 `learning_signals`로 파생 후 조회 |
| `learning_signals` | `REASON`, `GOOD_PATTERN`, `BAD_PATTERN`, `CORRECTION`, `VERIFIED_FACT`의 구조화된 신호 본문, scope, topic, 상품 identity, promotion 상태 | **구조화 신호 source-of-truth**, 원 Positive/Negative 본문의 파생 데이터 | `id` | `source_key UNIQUE`; feedback/example/historical/inquiry FK 모두 `SET NULL`; `normalized_identity_key`로 동일 주장 집계 | `services/learning_signal_service.py:LearningSignalService.capture` 및 자동 추출 경로 → `repositories/learning_signal_repository.py:LearningSignalRepository.upsert` | `LearningSignalService.retrieve` → `LearningContextService.build`; Manager 조회 |
| `learning_signal_confirmations` | 한 signal을 뒷받침한 독립 source 행, 활성/취소와 source authority | **confirmation ledger**; 본문 아님 | `id` | `UNIQUE(learning_signal_id, confirmation_key)`; signal `CASCADE`, example/feedback/inquiry `SET NULL`; `approval_history_id`에는 FK가 없음 | `LearningSignalRepository.record_confirmation`, `deactivate_confirmations_for_learning_example`, `deactivate_confirmations_for_learning_feedback` | `LearningSignalRepository.candidates/live_confirmation_count` → `LearningSignalService._eligible` |
| `answer_drafts` | 프로그램 원본, 직원 수정본, 승인 `final_answer`, review/post 상태 | **승인 답변 본문 현재본**(게시 전/내부 승인 기준) | `id` | `inquiry_id NOT NULL ON DELETE CASCADE`; inquiry별 `is_active`로 현재 draft 선택 | `repositories/answer_repository.py:AnswerRepository.create_program_draft`, `save_edited_answer`; `repositories/approval_repository.py:ApprovalRepository.approve/cancel_approval` | Review/approval UI, 게시 workflow, `LearningService.capture_approved`; 생성 시 provenance가 draft에 연결됨 |
| `inquiries` (직접 연결) | 문의 workflow 및 현재 승인 상태 | **승인 상태 source-of-truth** | `id` | source 문의 식별자들 | `ApprovalRepository.approve/cancel_approval`가 `approval_status`, `approved_at`, `approved_by` 갱신 | `ApprovalRepository.get_inquiry_approval`, dashboard/workflow |
| `approval_history` | `EDIT_SAVED`, `APPROVED`, `APPROVAL_CANCELLED`, `RESET` 이벤트와 전/후 상태·actor·사유 | **HISTORY_SNAPSHOT / event ledger** | `id` | inquiry `CASCADE`; draft `SET NULL` | `ApprovalRepository.record_action`, `approve`, `cancel_approval` | `ApprovalRepository.history_for_inquiry`; Positive Learning의 `approval_history_id` 연결 |
| `naver_posted_answers` (직접 연결) | Naver에서 관측된 실제 고객 노출 답변과 current 상태 | **실제 게시 답변 source-of-truth** | `id` | `source_key UNIQUE`; inquiry `CASCADE`; `is_current` | `NaverPostedAnswerRepository`와 Naver 답변 조회/동기화 경로 | `LearningFeedbackService`의 평가 대상 선택, `LearningService.capture_verified_posted_answer` |
| `historical_cases` | import된 과거 문의/판매자 답변 현재본, 품질·위험·active·승격 링크 | **Historical Answer source-of-truth** | `id` | `case_key UNIQUE`, `fingerprint UNIQUE`; inquiry `SET NULL`; `promoted_learning_id` → learning example `SET NULL` | `services/historical_case_service.py:HistoricalCaseService.import_local/import_naver` → `HistoricalCaseRepository.upsert`; `set_learning_enabled`, `mark_promoted` | `HistoricalCaseService.search_detailed/search` → `LearningContextService.build`; Historical Manager |
| `historical_case_versions` | Historical Case fingerprint별 답변·품질·위험 snapshot | **HISTORY_SNAPSHOT** | `id` | `fingerprint UNIQUE`; `historical_case_id NOT NULL ON DELETE CASCADE` | `HistoricalCaseRepository.upsert`의 `INSERT OR IGNORE` | production 조회 함수 없음. 현재는 저장만 되는 감사 snapshot |
| `answer_learning_provenance` | Learning/Historical reference의 retrieval run, prompt 부착, 실제 사용 판정 | **PROVENANCE_ONLY** | `id` | inquiry `CASCADE`, draft `SET NULL`, learning/historical `CASCADE`; run+reference unique | `LearningContextService.build` → `repositories/learning_provenance_repository.py:LearningProvenanceRepository.record_context`; `AnswerRepository.create_program_draft` → `attach_latest_context/finalize_for_draft` | Learning performance/dashboard와 감사 조회 |
| `answer_feedback_signal_provenance` | Structured Signal의 prompt 부착, conflict, provider claim, system-verified usage | **PROVENANCE_ONLY** | `id` | inquiry `CASCADE`, draft `SET NULL`, signal `CASCADE` | `LearningContextService.build` → `FeedbackSignalProvenanceRepository.record_context`; `AnswerRepository.create_program_draft` → `attach_latest_context/finalize_for_draft` | Learning performance/dashboard와 감사 조회 |

`learning_candidates`는 `repositories/database.py` migration 1에 PK `id`, inquiry `CASCADE`, draft `SET NULL`로 정의되어 있으나 production repository/service/UI의 SELECT/INSERT/UPDATE/DELETE가 전혀 없다. 현재 source-of-truth가 아니며 `learning_examples` 이전의 dead schema로 판정한다.

## [Positive Learning Lifecycle]

### 직원 승인 경로

`ui/review_workspace.py`의 승인 동작
→ `services/approval_service.py:ApprovalService.save_edited_answer`가 `AnswerRepository.save_edited_answer`로 `answer_drafts.edited_answer` 저장, `ApprovalRepository.record_action`으로 `approval_history(EDIT_SAVED)` 기록
→ `ApprovalService.approve`
→ `ApprovalRepository.approve`가 하나의 transaction에서 `answer_drafts.final_answer/review_status`, `inquiries.approval_status/approved_at/approved_by`, `approval_history(APPROVED)` 저장
→ `LearningService.capture_approved`
→ `LearningRepository.upsert_human_verified_atomic`
→ `learning_examples`에 마스킹된 별도 Positive snapshot 저장
→ `LearningSignalService.capture`가 선택적으로 구조화 신호를 `learning_signals`에 저장하고 `learning_signal_confirmations`에 근거 연결
→ `LearningRepository.candidates`가 `active=1`, `validity_active=1`, 유효기간 내, `POSITIVE`만 조회
→ `SimilarAnswerService.context/search`
→ `LearningContextService.build`
→ `DraftGenerationService`/`GptGovernanceService`의 prompt context와 답변 검증에 사용
→ `LearningProvenanceRepository.record_context/finalize_for_draft`가 `answer_learning_provenance`에 부착/실사용 결과 기록.

`LearningService.capture_approved`는 승인 transaction 뒤에 호출되며 실패가 승인을 되돌리지 않는 관측/학습 보조 경로이다. 따라서 승인 완료와 Positive Learning 생성은 동일 transaction이 아니다.

### 그 밖의 Positive 생성 경로

- 실제 게시 답변: `LearningService.capture_verified_posted_answer`가 `naver_posted_answers.is_current=1`의 고객 노출 답변을 읽어 `learning_examples(SELLER_ANSWER)` snapshot으로 저장한다.
- 자동 게시 관측/수정: `LearningService.capture_auto_post_version`이 `answer_versions`의 허용된 posted version을 검증해 `learning_examples(AUTO_POST_CORRECTED/AUTO_POST_REVIEWED_NO_CHANGE)`로 저장한다.
- Historical 승격: `HistoricalCaseService.promote` → `LearningService.capture_historical_promotion`이 `historical_cases.question/seller_answer`를 `learning_examples`에 복사하고 → `HistoricalCaseRepository.mark_promoted`가 `historical_cases.promoted_learning_id`를 기록한다.

### 수정·비활성화

- 학습 본문을 직접 덮어쓰는 일반 편집 경로는 없다. 동일 `source_key`는 upsert되며, 답변 변경은 새 snapshot/새 source key 또는 기존 source의 명시적 upsert로 표현된다.
- 유효기간 수정: `ui/learning_manager.py` → `LearningRepository.update_validity` → `validity_type`, `valid_from`, `valid_until`, `validity_active`, `expired_at`.
- 승인 취소: `ApprovalService.cancel_approval_with_learning` → `ApprovalRepository.cancel_approval` + `LearningRepository.revoke_human_verified`; `learning_examples.active=0` 및 revoke metadata, 해당 signal confirmation도 비활성화.
- 자동 관측본 supersede: `LearningRepository.deactivate_automatic_positive`.
- draft 단위 비활성화 API: `LearningService.deactivate_draft` → `LearningRepository.deactivate_draft`. production 호출은 확인되지 않았다.
- physical DELETE production 경로: 없음.

## [Negative Learning Lifecycle]

`ui/review_workspace.py`의 직원 평가
→ `LearningFeedbackService.capture_dashboard_negative` 또는 `capture_dashboard_excluded`
→ 평가 대상은 `answer_drafts`의 PROGRAM/STAFF/FINAL 본문 또는 `naver_posted_answers`의 실제 게시 본문으로 확정
→ Positive/반대 평가 충돌을 검사
→ `LearningFeedbackRepository.save_dashboard_evaluation_atomic`
→ `learning_feedback`에 `NEGATIVE`, 필요 시 `INTENT_CORRECTION`, 또는 `EXCLUDED` 저장
→ exact rejected 자동 Positive가 있으면 `LearningFeedbackRepository._soft_revoke_matching_auto`가 해당 `learning_examples.active=0`
→ 선택적 구조화 내용은 `LearningSignalService.capture`로 `learning_signals` 및 confirmation에 저장
→ runtime에서 `LearningRepository.candidates`가 활성 feedback의 정확한 답변 identity를 차단하고, `LearningSignalService.retrieve`가 eligible 구조화 correction/pattern/fact만 `LearningContextService.build`에 공급
→ `FeedbackSignalProvenanceRepository.record_context/finalize_for_draft`가 signal 사용을 기록.

직원 수정 저장 경로는 `ApprovalService.save_edited_answer` → `LearningFeedbackService.capture_staff_correction`이며, 같은 draft의 기존 correction은 `deactivate_for_draft` 후 새 평가로 대체된다. Historical review는 `LearningFeedbackService.capture_historical_review`가 `historical_cases.active=0` 및 learning signal type을 설정하고 기존 review feedback을 비활성화한 뒤 새 feedback을 저장한다.

수정은 row 본문을 임의 편집하는 방식이 아니라 deterministic `source_key` upsert 또는 기존 활성 row 비활성화 후 새 평가 저장이다. Excluded는 `LearningFeedbackService.revoke_dashboard_excluded` → `LearningFeedbackRepository.revoke_dashboard_exclusion`으로 `active=0` 및 revoke metadata를 남긴다. Dashboard Negative용 `LearningFeedbackRepository.deactivate_dashboard_evaluation`은 구현되어 있으나 production 호출자가 없어 현재 UI/service revoke lifecycle은 **미구현**이다. physical DELETE는 없다.

## [승인 답변 Lifecycle]

문의 기반 프로그램 초안 생성
→ `DraftGenerationService`/`AnswerService`
→ `AnswerRepository.create_program_draft`
→ `answer_drafts.original_answer`
→ 직원 수정은 `ApprovalService.save_edited_answer` → `answer_drafts.edited_answer` + `approval_history(EDIT_SAVED)`
→ 직원 승인은 `ApprovalService.approve` → `ApprovalRepository.approve`
→ `answer_drafts.final_answer`, `answer_drafts.review_status='APPROVED'`, `inquiries.approval_status='APPROVED'`, `approval_history(APPROVED)`
→ 게시 workflow가 승인된 draft를 조회해 사용
→ 실제 게시 결과는 별도 게시/관측 테이블과 `naver_posted_answers`로 관리
→ 승인 취소는 `ApprovalService.cancel_approval_with_learning` → `ApprovalRepository.cancel_approval`으로 `final_answer=NULL`, draft `IN_REVIEW`, inquiry `PENDING`, `approval_history(APPROVAL_CANCELLED)`
→ 연결 Positive Learning도 soft revoke.

승인 답변 자체의 runtime retrieval은 게시 workflow와 Learning 생성의 입력으로 사용된다. 다음 새 답변의 유사 사례로 쓰이는 것은 `answer_drafts`를 직접 검색해서가 아니라, 승인 후 만들어진 `learning_examples`를 통해서다.

## [Historical Answer Lifecycle]

Local DB 또는 Naver source 조회
→ `HistoricalCaseService.import_local` / `import_naver`
→ privacy/품질/위험/identity 정규화
→ `HistoricalCaseRepository.upsert`
→ `historical_cases` 현재본 insert/update + fingerprint별 `historical_case_versions` snapshot insert
→ 조회/관리: `HistoricalCaseRepository.list_cases/get`, `ui/historical_case_manager.py`
→ 수정: 일반 본문 수동 편집 없음; 재import 시 동일 `case_key` 현재본 update, 새 fingerprint면 version 추가
→ 비활성화/재활성화: `HistoricalCaseRepository.set_learning_enabled` (`active`, metadata)
→ Negative/Excluded review: `LearningFeedbackService.capture_historical_review`
→ runtime retrieval: `HistoricalCaseRepository.candidates(active=1, seller_answer 존재)` → `HistoricalCaseService.search_detailed`의 품질·risk·상품 호환성·충돌 필터 → `LearningContextService.build`
→ 답변 생성 context에 사용
→ `answer_learning_provenance(reference_kind='HISTORICAL')` 기록
→ 선택적 관리자 승격: `HistoricalCaseService.promote` → `learning_examples` snapshot 생성 → `historical_cases.promoted_learning_id` 연결.

`historical_case_versions`를 조회하거나 복원하는 production 함수/UI는 없다. 버전은 보존되지만 현재는 write-only 감사 snapshot이다. Historical case의 physical DELETE production 경로도 없다.

## [승인 답변과 Positive Learning 관계]

세 테이블은 같은 것이 아니다.

- `answer_drafts`: 현재 draft의 원본/수정본/승인 본문을 보유한다. 승인 취소 시 `final_answer`가 지워지는 현재 상태 저장소이다.
- `approval_history`: 승인 과정의 event ledger이다. 답변 본문 전체를 저장하지 않으며 현재 승인 상태를 결정하지 않는다.
- `learning_examples`: 승인된 시점의 질문과 답변을 마스킹해 복제한 독립 학습 snapshot이다. 다음 문의 retrieval은 이 테이블을 읽는다.

승인 시 `answer_drafts.final_answer`가 `learning_examples.final_answer`로 **복제 저장**된다. 동시에 `learning_examples.answer_draft_id`와 `approval_history_id`로 원 draft와 승인 이벤트를 연결한다. 따라서 복제와 ID 연결을 모두 사용한다. 이는 runtime 학습을 승인 workflow row의 가변 상태·삭제 생명주기에서 분리하고, 마스킹·품질·scope·유효기간을 독립 관리하기 위한 `INTENTIONAL_DUPLICATION`이다.

승인 뒤 source-of-truth를 하나로 뭉뚱그리면 안 된다. 승인 본문 현재본은 `answer_drafts`, 승인 상태는 `inquiries`, 이벤트 이력은 `approval_history`, retrieval용 Positive는 `learning_examples`, 실제 고객 노출 답변은 `naver_posted_answers`가 각각 권위가 있다.

## [Signals / Provenance]

### `historical_cases.promoted_learning_id`

관리자가 Historical case를 승격하면 Historical 질문/답변 snapshot이 `learning_examples`에 생성되고, 생성된 PK가 `historical_cases.promoted_learning_id`에 저장된다. FK는 `ON DELETE SET NULL`이다. `LearningContextService.build`는 Learning reference의 `historical_case_id` metadata를 이용해 동일 promoted Historical case가 Learning과 Historical 양쪽으로 동시에 붙는 것을 제거한다.

### 중복 분류

| 구조 | 분류 | 근거 |
|---|---|---|
| `answer_drafts.final_answer` → `learning_examples.final_answer` | `INTENTIONAL_DUPLICATION` | 승인 당시의 마스킹된 retrieval snapshot과 독립 lifecycle 확보 |
| `naver_posted_answers.answer_body` → `learning_examples(SELLER_ANSWER)` | `INTENTIONAL_DUPLICATION` | 고객 노출 원본과 human-verified retrieval snapshot의 책임 분리 |
| `historical_cases.seller_answer` → promoted `learning_examples.final_answer` | `INTENTIONAL_DUPLICATION` | Historical archive와 Positive retrieval lifecycle 분리; ID/metadata 연결 존재 |
| Local historical import가 `learning_examples(SELLER_ANSWER)`를 읽어 `historical_cases`에 저장 | `INTENTIONAL_DUPLICATION` | 시점이 있는 Historical corpus materialization. 단, 비승격 동일 사례의 양쪽 retrieval 중복 가능성은 LOW 항목으로 남김 |
| `learning_feedback`의 마스킹 질문/원답/교정답 | `HISTORY_SNAPSHOT` | 평가 시점의 대상 identity와 감사 근거 보존 |
| `historical_case_versions` | `HISTORY_SNAPSHOT` | fingerprint별 변경 전후 snapshot |
| `learning_signals.content_text` | `DERIVED_DATA` | 원 Learning 답변 전체가 아니라 독립 ranking 가능한 구조화 주장/패턴 |
| `learning_signal_confirmations` | `PROVENANCE_ONLY` | signal 본문 없이 source row와 확인/revoke 상태만 저장 |
| `answer_learning_provenance` | `PROVENANCE_ONLY` | reference ID, prompt 부착, 사용 상태만 저장 |
| `answer_feedback_signal_provenance` | `PROVENANCE_ONLY` | signal ID, conflict/coverage/사용 판정만 저장 |
| `approval_history` | `HISTORY_SNAPSHOT` | 승인 상태 전이 event; 답변 본문 복제 없음 |

필요성 없이 동일 책임의 canonical row가 둘 이상 존재하는 `ACTUAL_DUPLICATE`는 확인되지 않았다.

## [삭제 / 비활성화 계약]

| 데이터 | 실제 운영 lifecycle | physical DELETE / FK 결과 | retrieval 제외 |
|---|---|---|---|
| `learning_examples` | `active=0` revoke/supersede/deactivate; `validity_active=0`; TEMPORARY 기간 만료 | production DELETE 없음. inquiry/draft/approval 삭제 시 FK는 `SET NULL`이라 본문 보존 | `LearningRepository.candidates`가 inactive, validity inactive, 시작 전·만료 후를 제외 |
| `learning_feedback` | 새 staff/historical 평가 전 기존 행 `active=0`; Excluded revoke; Negative repository deactivate 함수 존재 | production DELETE 없음. 연결 원본 삭제 시 FK `SET NULL` | exact 차단과 manager의 active query에서 제외. 연결 signal confirmation도 일부 revoke 경로에서 비활성화 |
| `learning_signals` | `deactivate(active=0)`, `reject(REJECTED + active=0)`, `promote(MANUALLY_PROMOTED)` | production DELETE 없음. 원 feedback/example/historical/inquiry 삭제 시 FK `SET NULL`; signal 자체 물리 삭제 시 confirmations와 signal provenance `CASCADE` | candidates가 `active=1`이고 `REJECTED/SUPERSEDED`가 아닌 것만 반환; factual auto signal은 live confirmation threshold 적용 |
| `learning_signal_confirmations` | source 승인/feedback 취소 시 `active=0`, revoked reason/time | signal 물리 삭제 시 `CASCADE`; example/feedback/inquiry 삭제 시 `SET NULL`; `approval_history_id` FK 없음 | live confirmation count가 confirmation과 source row 모두 active인지 검사 |
| `answer_drafts` | `is_active` 전환; 승인 취소 시 final clear/review state 변경 | inquiry 물리 삭제 시 `CASCADE`; 별도 production DELETE 없음. golden-run 진단 script에는 명시적 삭제가 있으나 운영 lifecycle이 아님 | 현재 draft 조회는 `is_active` 우선 |
| `approval_history` | append-only event; revoke는 새 `APPROVAL_CANCELLED` 행 | inquiry 삭제 `CASCADE`, draft 삭제 `SET NULL` | 현재 상태 판단용이 아니라 이력 조회용 |
| `historical_cases` | `active=0/1` learning toggle; review가 signal type metadata 설정 | production DELETE 없음. 물리 삭제 시 versions와 Historical provenance `CASCADE`, promoted link의 상대 방향은 learning 삭제 시 `SET NULL` | candidates가 `active=1`과 답변 존재를 요구하고 service 품질/risk/conflict 필터 적용 |
| `historical_case_versions` | append-only fingerprint snapshot | parent Historical case 물리 삭제 시 `CASCADE` | runtime retrieval에 직접 참여하지 않음 |
| 두 provenance 테이블 | 상태 finalize로 `USED/NOT_USED/...` 기록 | inquiry 및 참조 원본의 물리 삭제에 일부 `CASCADE`; draft 삭제는 `SET NULL` | retrieval 근거가 아니라 사후 추적 자료 |

정리하면, 정상 UI/service 경로에서는 Learning 본문을 physical DELETE하지 않고 soft lifecycle을 쓴다. soft deactivate/revoke 상태에서는 provenance와 Historical versions가 남는다. 그러나 schema상 참조 원본이나 inquiry를 직접 물리 삭제하면 `CASCADE`인 provenance/version은 남지 않으므로, “어떤 종류의 삭제에서도 provenance가 영구 보존된다”는 계약은 아니다.

유효기간은 `learning_examples`에만 명시적으로 구현되어 있다. `PERMANENT`는 활성인 동안 사용되고, `TEMPORARY`는 `valid_from <= now <= valid_until`이며 `validity_active=1`이어야 한다. 만료 또는 비활성 Learning은 runtime retrieval에서 제외된다. Negative feedback, Historical case, signal에는 동일한 날짜 만료 축이 없다.

## [정적 JSON vs SQLite]

### 정적 파일

현재 `answer_data/learning/`에는 `model_data_with_color.json` 하나만 있다. 이 파일은 `answer/config_loader.py:load_answer_config/_load_cached`가 읽어 `AnswerConfig.model_catalog`으로 만들고, `answer/engine.py:AnswerEngine._load_model_catalog` 및 모델 catalog 규칙이 사용한다.

- 생성 주체: 현재 runtime은 생성/수정하지 않는다. repository에 배포된 정적 자료이다.
- 읽는 주체: `answer.config_loader`, `AnswerEngine`.
- 성격: **reference/static product model catalog**. bootstrap Learning, fallback Learning, Positive/Negative Learning 저장소가 아니다.
- 우선순위: SQLite Learning과 동일 key로 우선순위를 경쟁하지 않는다. 정적 규칙/상품 catalog와 동적 Learning context가 서로 다른 입력 계층이다.
- 운영 중 새 Learning 저장 위치: SQLite의 `learning_examples`, `learning_feedback`, `learning_signals`와 연결 테이블들이다. JSON에 쓰지 않는다.
- 동일 데이터 중복: 모델/상품 문구가 Learning 답변에 일부 나타날 수는 있으나 동일 record를 동기화하는 이중 저장 계약은 없다.

### 운영 SQLite

`repositories/database.py` 기준 기본은 `data/oje_automation.db`, 환경 override는 `OJE_AUTOMATION_DB_PATH`이다. `app.py`는 `Database()`를 생성하며 runtime Learning service/repository가 이 연결을 공유한다. Positive, Negative, Historical, signal, provenance가 실제로 생성·조회·상태 변경되는 운영 저장소다.

## [Legacy 구조]

- `outputs/learning`: 현재 directory가 없고 production 참조가 없다. 과거 `qna_auto/learning.py`가 JSONL/XLSX를 쓰던 구조로 `docs/AUTO_QNA_SOURCE_ANALYSIS.md`에만 이력이 남아 있다. **LEGACY**.
- `configuration.xlsx`: repository에 파일이 없고 현재 `answer/config_loader.py`는 읽지 않는다. `answer/engine.py`의 문자열은 채택 사유/legacy category 문구이며 workbook I/O가 아니다. `scripts/audit_legacy_answer_templates.py`는 감사 script일 뿐 runtime writer가 아니다. **LEGACY**.
- `naver_workflow/qna_auto_runner.py`: `학습검수대기.xlsx`를 `configuration.xlsx`에 반영한다는 legacy 독립 runner 문구가 남아 있지만 현재 app/service에서 import되지 않는다. **LEGACY / isolated tool**.
- `model_data_with_color.json`: 현재 runtime에서 살아 있으나 Learning 저장소가 아닌 **ACTIVE STATIC REFERENCE**.
- `learning_candidates`: schema와 과거 문서만 남고 production 경로가 없다. **DEAD SCHEMA**.

## [전체 데이터 흐름도]

```text
고객 문의 (inquiries)
  │
  ├─ 답변 생성
  │    ├─ 정적 정책/모델 catalog (answer_data/*.json)
  │    ├─ Positive retrieval (learning_examples)
  │    ├─ Historical retrieval (historical_cases)
  │    └─ Structured Signal retrieval (learning_signals
  │                                      ← learning_signal_confirmations)
  │              │
  │              └─ LearningContextService.build
  │                    ├─ answer_learning_provenance (부착/사용 추적)
  │                    └─ answer_feedback_signal_provenance (부착/사용 추적)
  │
  └─ answer_drafts.original_answer
         │
         ├─ 직원 수정 → answer_drafts.edited_answer
         │               ├─ approval_history(EDIT_SAVED)
         │               └─ correction 평가 → learning_feedback
         │                                      └─ 파생 signal → learning_signals
         │
         └─ 직원 승인
              ├─ inquiries.approval_status = APPROVED   [현재 승인 상태]
              ├─ answer_drafts.final_answer             [현재 승인 본문]
              ├─ approval_history(APPROVED)              [이벤트 이력]
              └─ Positive snapshot → learning_examples  [독립 retrieval source]
                                       └─ optional signal/confirmation

실제 Naver 게시/관측 답변
  └─ naver_posted_answers(is_current=1)                 [고객 노출 원본]
       └─ 직원 검증 시 SELLER_ANSWER snapshot → learning_examples

과거 문의/판매자 답변 import
  └─ historical_cases                                   [현재 Historical 본문]
       ├─ 변경 fingerprint → historical_case_versions   [history snapshot]
       ├─ Negative/Excluded review → learning_feedback
       └─ 관리자 승격 → learning_examples
                          ↑
             historical_cases.promoted_learning_id
```

## [중복 저장 분석]

현재 중복은 대부분 snapshot과 책임 분리를 위한 의도된 중복이다. `source_key`, `case_key`, `fingerprint`, `normalized_identity_key`, confirmation key가 같은 계층 내부의 중복을 제한한다. 승인 답변은 복제와 FK 연결을 함께 사용하며, Historical 승격도 양방향 식별 가능한 연결을 남긴다.

한 가지 경계는 `HistoricalCaseService.import_local`이 `learning_examples(SELLER_ANSWER)`를 Historical corpus로 다시 materialize할 수 있다는 점이다. promoted case는 `LearningContextService`가 중복 prompt 부착을 제거하지만, 승격되지 않은 동일 source 답변을 Learning과 Historical 양쪽에서 찾는 경우를 통합 source identity로 제거하는 일반 규칙은 확인되지 않았다. 저장 목적은 다르므로 `INTENTIONAL_DUPLICATION`으로 분류하되, retrieval 관점의 중복 후보는 LOW로 기록한다.

## [문제 목록]

| 등급 | 항목 | 영향 / 근거 | 이번 작업 처리 |
|---|---|---|---|
| MEDIUM | Dashboard Negative revoke lifecycle이 끝까지 연결되지 않음 | `LearningFeedbackRepository.deactivate_dashboard_evaluation`과 confirmation revoke SQL은 있으나 production service/UI 호출이 없다. Excluded revoke 및 승인 취소와 비교해 Negative 평가 취소 경로가 비대칭이다. 안전한 차단이 남는 방향이므로 즉시 안전 결함은 아니다. | 수정하지 않음 |
| LOW | `learning_signal_confirmations.approval_history_id`에 FK 없음 | 승인 이력 물리 삭제 시 orphan scalar ID가 남을 수 있다. example/feedback/inquiry 연결과 source active JOIN은 별도로 있으므로 현재 eligibility를 직접 깨지는 않는다. | 수정하지 않음 |
| LOW | `historical_case_versions` 조회/복원 경로 없음 | snapshot은 저장되지만 production repository/service/UI에서 읽지 않는다. 감사/복원 기능이 완결되지 않았다. | 수정하지 않음 |
| LOW | Local SELLER_ANSWER의 두 retrieval store 노출 가능성 | `learning_examples`에서 Historical로 materialize된 동일 답변이 promoted link 없이 두 후보군에 존재할 수 있다. 보편 dedup 식별자는 확인되지 않았다. | 수정하지 않음 |
| LOW | `learning_candidates` dead schema | production read/write가 없고 현재 Learning source가 아니다. 운영 혼동과 schema 부채만 남긴다. | 수정하지 않음 |
| INFO | `answer_data/learning` 명칭 혼동 | 내부 JSON은 동적 Learning이 아니라 모델 catalog다. | 본 보고서에서 역할 확정 |
| INFO | soft lifecycle과 FK cascade 보존 범위가 다름 | 정상 revoke에서는 provenance가 남지만 직접 physical parent delete에서는 일부 cascade된다. | 본 보고서에서 계약 명시 |

- CRITICAL: 0
- HIGH: 0
- MEDIUM: 1
- LOW: 4
- INFO: 2

## [Negative Learning 후속 작업에 미치는 영향]

향후 Negative Learning을 개선할 때 수정 대상은 새 저장소나 schema 설계가 아니라 다음 정확한 계층이다.

1. UI: `ui/review_workspace.py`에 Negative 평가 취소 동작을 Excluded 취소와 동일한 권한·사유 입력 계약으로 연결한다.
2. Service: `services/learning_feedback_service.py:LearningFeedbackService`에 명시적인 dashboard Negative revoke method를 두어 대상/상태/actor/reason을 검증한다.
3. Repository: 기존 `repositories/learning_feedback_repository.py:LearningFeedbackRepository.deactivate_dashboard_evaluation`을 재사용하거나 ID 단위 transaction으로 좁히고, 영향받는 `learning_signal_confirmations`만 함께 revoke한다.
4. Retrieval 계약: `LearningRepository.candidates`의 active feedback exact-answer 차단과 `LearningSignalRepository`의 live confirmation 계산이 revoke 직후 자동으로 반영되는지 focused test로 고정한다.
5. Provenance: 과거 `answer_feedback_signal_provenance`는 감사 이력이므로 수정하지 않고 남기며, source soft revoke 이후 새 context에는 해당 signal이 붙지 않는지만 보장한다.

`learning_examples`, Historical schema, 정적 JSON, 새 DB migration을 건드릴 이유는 현재 조사에서 발견되지 않았다.

## [최종 판정]

1. 현재 Learning primary source-of-truth: 운영 SQLite(`OJE_AUTOMATION_DB_PATH`, 기본 `data/oje_automation.db`)의 역할별 canonical 테이블.
2. Positive Learning source-of-truth: `learning_examples`.
3. Negative Learning source-of-truth: `learning_feedback`; runtime용 구조화 파생 신호의 canonical table은 `learning_signals`.
4. 승인 답변 source-of-truth: 승인 본문은 `answer_drafts.final_answer`, 현재 승인 상태는 `inquiries.approval_status/approved_at/approved_by`; `approval_history`는 이력. 실제 게시 답변은 `naver_posted_answers`가 별도 권위.
5. Historical Answer source-of-truth: `historical_cases`; `historical_case_versions`는 history snapshot.
6. runtime retrieval이 읽는 저장소: `learning_examples`, `historical_cases`, `learning_signals`/`learning_signal_confirmations`. `learning_feedback`은 exact exclusion/conflict gate에도 직접 사용된다. 정적 모델 catalog는 별도 rule input이다.
7. 불필요한 실제 중복 저장: 확인되지 않음. 의도된 snapshot 중복은 존재하며 Local Historical materialization의 후보 중복 가능성은 LOW.
8. orphan/dead/legacy 구조: `learning_candidates` dead schema, `outputs/learning`·`configuration.xlsx` legacy, `historical_case_versions`는 active write-only snapshot, confirmation의 비-FK `approval_history_id`는 잠재 orphan.
9. 삭제/비활성화 정책 일관성: 안전 방향의 soft lifecycle은 일관적이나, Negative revoke UI/service 부재와 physical FK cascade 시 provenance 보존 범위 차이 때문에 완전히 균일하지는 않다.
10. Negative Learning 개선 시 수정 계층: `ui/review_workspace.py` → `LearningFeedbackService` → `LearningFeedbackRepository.deactivate_dashboard_evaluation` 및 signal confirmation revoke → retrieval focused test. 새 schema는 불필요하다.

CRITICAL/HIGH가 없고, 요청된 6개 미확인 사항은 코드 근거로 확정됐다.

**LEARNING STORAGE AUDIT COMPLETE**
