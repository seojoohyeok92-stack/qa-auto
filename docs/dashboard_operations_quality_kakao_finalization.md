# Q&A Auto Dashboard 운영 UI · 답변 품질 KPI · Kakao 표시 최종 통합

## [요약]

- 시작 기준은 `main`의 `8088c4cd4ff65404e7c7e73260a668ab4733cd15`이며 working tree는 clean이었다.
- 자동처리와 관리자 모드의 시작/종료 버튼을 각각 상태 기반 단일 버튼으로 정리했다.
- 관리자 모드 OFF에서는 관리 영역 5개를 렌더링하지 않고, 네이버 문의 동기화는 항상 표시한다.
- 기본 품질 화면을 자동 답변 생성률, 자동 등록률, 직원 수정률, 직원 검토 필요율과 직원 수정률 추이로 단순화했다.
- 실제 Naver 등록 성공이 아닌 Kakao 알림은 모두 `답변: -`로 표시한다.
- DB/schema/migration, 운영 데이터, Auto Processing/Post, Naver/Kakao outbox·retry·dedup 동작은 변경하지 않았다.
- focused tests `225 passed / 0 failed`, 전체 suite `3633 passed / 0 failed / 0 skipped`이다.

## [시작 상태]

- repository: `C:\Users\user\Desktop\프로젝트\Q&A 통합\git\qa-auto`
- branch: `main`
- HEAD: `8088c4cd4ff65404e7c7e73260a668ab4733cd15`
- 시작 working tree: clean
- 예상 HEAD와 실제 HEAD: 일치
- 사용자 unrelated 변경: 없음

## [상단 운영 UI 기존 구조]

`ui/production_dashboard.py`는 자동처리와 관리자 모드에 각각 시작/종료 버튼 두 개를 렌더링했다. Auto Processing은 `AutoPostRuntimeService.enable()/disable()`, 관리자 모드는 `DashboardPreferencesRepository.save_admin_mode()`를 사용했다. 자동처리 시작 dialog와 안전 종료 계약은 이미 구현돼 있었다.

## [자동처리 단일 버튼]

- OFF: `자동처리`
- ON: `● 자동처리`
- OFF 클릭: 기존 `_confirm_auto_post_start()` dialog를 거쳐 확인 후 `AutoPostRuntimeService.enable()` 호출
- ON 클릭: 기존 `AutoPostRuntimeService.disable()` 호출
- ON 색상: 기존 dark theme에 맞춘 절제된 green `#26734d`
- 환경 잠금, 시작 confirmation, 진행 중 POST 안전 종료 계약은 변경하지 않았다.

## [관리자 모드 단일 버튼]

- OFF: `관리자 모드`
- ON: `● 관리자 모드`
- 동일 버튼이 현재 상태의 반대 상태를 `DashboardPreferencesRepository`에 저장한다.
- ON 색상: dark theme에 맞춘 purple `#5b4bb7`
- Auto Processing 상태와 독립된 UI visibility 설정이다.

## [관리자 모드 Visibility]

관리자 OFF에서 다음 5개 영역을 렌더링하지 않는다.

1. 실시간 운영 상태
2. 관리자 Scheduler · 상세 설정
3. 오늘 운영 통계
4. Learning Repository
5. 관리자 상세

`네이버 문의 동기화`는 관리자 모드와 무관하게 항상 표시한다. 관리자 ON에서는 5개 관리 영역과 Naver Sync가 모두 표시된다. 자동처리 ON + 관리자 OFF 조합도 유지된다.

## [답변 품질 KPI 정의]

모든 기간 KPI는 `repositories/learning_performance_repository.py`의 SQL aggregation으로 계산한다. Dashboard rerun 시 전체 inquiry row를 Python으로 materialize하지 않는다. 기간은 최근 7일, 30일, 90일이며 직전 동일 길이 기간을 비교한다.

비율 변화는 `%p`로 표시한다. 생성률·등록률 상승은 개선, 수정률·검토율 하락은 개선이다. 현재 또는 이전 분모가 없으면 `0.0%` 대신 `측정 데이터 부족`을 표시한다.

## [자동 답변 생성률]

- 분모: 기간 내 `activity_logs.event_code='AUTO_ANSWER_STARTED'`인 distinct inquiry
- 분자: 같은 처리 cohort 중 기간 내 `AUTO_ANSWER_SUCCEEDED`가 확인된 distinct inquiry
- 제외/실패: 정책 차단, 생성 실패, 검토 전환, 빈/오류 등은 성공 분자에서 제외하되 시작된 처리는 분모에 포함
- 최근 30일 실제값: `20 / 92 = 21.7%`
- 이전 30일: 처리 표본 0건으로 측정 데이터 부족

단순 `answer_drafts` row 존재는 생성 성공으로 계산하지 않는다.

## [자동 등록률]

- 분모: 자동 답변 생성률과 같은 처리 cohort
- 분자: `naver_post_attempts.status='POSTED'`이며 `auto_post_run_id IS NOT NULL`인 distinct inquiry
- 수동 등록, POST 시도, POST 실패/불명은 성공에서 제외
- 최근 30일 실제값: `15 / 92 = 16.3%`
- 이전 30일: 처리 표본 0건으로 측정 데이터 부족

## [직원 수정률]

- source-of-truth: `post_reviews`, `answer_versions`
- 분모: Naver 등록이 확인된 `AUTO_POST_INITIAL` 중 결과가 `UNCHANGED` 또는 `CORRECTED`로 판정 완료된 표본
- 분자: `NAVER_CORRECTION_APPLIED` 또는 `learning_examples.learning_source='AUTO_POST_CORRECTED'`가 있는 표본
- `PENDING`은 미수정으로 간주하지 않는다.
- 최근 30일: corrected 0건, 판정 완료 0건, 관찰 중 6건이므로 **측정 데이터 부족**
- 이전 30일: 판정 완료 0건으로 측정 데이터 부족

기존 Dashboard의 `직원 수정률 0.0%`는 실제 수정 0%가 아니라 **B. 수정 감지 데이터 부족 + D. 기간/표본 문제**였다. 이번 UI는 이를 0%로 위장하지 않는다.

## [직원 검토 필요율]

- 분모: 기간 내 자동처리 시작 distinct inquiry
- 분자: 다음 durable event가 확인된 distinct inquiry
  - `AUTO_PROCESSING_REVIEW_REQUIRED`
  - `AUTO_PROCESSING_BLOCKED`
  - `AUTO_POST_BLOCKED_DPS_SESSION`
  - `AUTO_POST_SKIPPED_POLICY_BLOCKED`
- 최근 30일 실제값: `71 / 92 = 77.2%`
- 이전 30일: 처리 표본 0건으로 측정 데이터 부족

## [Naver 직접 수정 감지 조사]

현재 `InquirySyncService`는 sync 응답의 `seller_answer`/`posted_answer`를 다시 읽고 `NaverPostedAnswerRepository.observe()`에 저장한다. 기존 Q&A Auto 등록 답변과 다른 remote seller answer를 감지하면 `PostReviewRepository.capture_remote_naver_edit()`가 `answer_versions.version_kind='NAVER_CORRECTION_APPLIED'`, `finalization_source='NAVER_DIRECT_EDIT_SYNC'` 이력을 남긴다.

따라서 감지된 Naver 직접 수정은 직원 수정률의 `CORRECTED`에 포함된다. 이후 기존 `LearningService.capture_auto_post_version(source='AUTO_POST_CORRECTED')` 경로가 조건부로 Learning에 연결한다. 이번 작업에서 새 자동 Learning 기능은 만들지 않았다.

개발 DB 현재 실적은 `NAVER_CORRECTION_APPLIED 0건`, `NAVER_DIRECT_EDIT_SYNC 0건`, `AUTO_POST_CORRECTED Learning 0건`이다. 구현 가능성과 회귀는 기존 fixture로 검증했지만 실제 수정 실적은 아직 없다.

## [직원 수정률 추이]

- 최근 7일/30일: KST 일별 aggregation
- 최근 90일: KST 주별 aggregation
- series: 직원 수정률 하나만 표시
- 최소 2 point 미만이면 chart 대신 데이터 부족 안내 표시
- 개발 DB 최근 30일: 판정 완료 point 0개이므로 데이터 부족 표시

## [기간 비교]

- 최근 7일 vs 직전 7일
- 최근 30일 vs 직전 30일
- 최근 90일 vs 직전 90일
- 상대 변화율이 아니라 percentage point를 사용한다.
- 개선/악화/변화 없음과 이전 기간 데이터 부족을 각각 구분한다.

## [데이터 부족 처리]

분모 0, 직원 수정 판정 완료 표본 0, 추이 point 2개 미만을 모두 별도 처리한다. 실제 개발 DB의 수정 관찰 6건은 `PENDING`이며 0%로 표시하지 않는다.

## [상세 분석 보존]

기존 기간 비교, Learning 참고 효과, 문의 유형별 품질, Learning 출처별 현황, Positive Learning 관찰 현황, Copilot 교정 Learning, 활성/신규/참조 현황은 `상세 분석` expander 아래에 보존했다. 기본 상태는 collapsed다.

## [Kakao 미등록 답변 표시]

`kakao_notify.py::format_qna_message()`에서 다음 표시 계약을 적용했다.

- `action == 'posted'`: `답변: <실제 등록 답변>`
- 그 외: `답변: -`

직원 검토, Validator 차단, Auto-post Gate 차단, draft-only, generation block, POST 실패, API 오류, dry-run은 모두 draft/final text를 답변으로 노출하지 않는다. 기존 미등록 사유, 답변 생성 상태, Naver 등록 상태는 유지한다.

## [Kakao 등록 성공 판정]

답변 text 존재 여부가 아니라 실제 POST 결과로 결정된 `action='posted'`만 성공으로 인정한다. 기존 legacy workflow도 POST 결과가 `posted`일 때만 이 action을 사용한다. outbox enqueue, retry, deduplication, recipient, title, 상품명, 문의와 reason routing은 변경하지 않았다.

## [Focused Tests]

- 결과: `225 passed / 0 failed`
- 범위: Dashboard rendering, 단일 toggle, confirmation, 안전 종료, 관리자 visibility, KPI SQL aggregation, 7/30/90일, 이전 기간 비교, 개선/악화/변화 없음, 데이터 부족, 직원 수정/직접 수정, Auto Post provenance, Naver Sync, Kakao formatting/outbox, 기존 Learning 통계
- 실제 외부 API 호출: 0

## [전체 Tests]

- 결과: `3633 passed / 0 failed / 0 skipped`
- 실행 시간: 약 19분 39초
- 현재 working tree 기준 전체 Q&A Auto test suite 성공

## [Safety]

- Auto Sync regression: 0
- Auto Processing regression: 0
- Auto Post 및 안전 종료 regression: 0
- 관리자 모드 regression: 0
- Naver Sync regression: 0
- DPS/DPS Keepalive regression: 0
- Answer Engine/Validator regression: 0
- Learning/Historical/Product Facts regression: 0
- Kakao routing/outbox/retry/dedup regression: 0
- Learning Manager/Excel Export regression: 0
- schema/migration 변경: 0
- 운영 DB write: 0
- 외부 실제 Provider/API 호출: 0

## [잔여 한계]

- CRITICAL: 0
- HIGH: 0
- MEDIUM: 0
- LOW: 1

LOW 1건은 직원 수정 판정 완료 및 이전 30일 표본이 아직 없어 수정률·추이를 수치화할 수 없다는 데이터 축적 한계다. 시스템은 이를 `측정 데이터 부족`으로 정확히 표시하며, schema나 새 Learning 기능이 필요한 문제는 아니다.

## [최종 판정]

**DASHBOARD OPERATIONS & QUALITY READY**

**— 운영 UI · 답변 품질 KPI · Kakao 미등록 표시 최종 정리 완료**

READY 조건을 모두 충족했다. 서버 작업과 실제 외부 API 호출은 수행하지 않았다.
