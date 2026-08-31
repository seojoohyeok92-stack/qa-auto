# Learning Manager 검색 최종 보완

## [요약]

Learning Manager의 최근 2,000건 선조회 구조를 repository-level 검색·필터·count·pagination 구조로 교체했다. Positive와 Feedback 모두 DB 전체를 검색 대상으로 삼되, Python에는 현재 page의 최대 100건만 반환한다. source/provenance option은 전체 행을 읽지 않고 `DISTINCT` query로 생성한다.

기존 검색 field는 유지하면서 `seller_answer`, `edited_answer`, `gpt_draft`, `valid_from`, `valid_until`, `condition_json`을 추가했다. `condition_json`은 이미 관리자 상세 화면에서 전체 표시되는 `learning_examples` 관리 metadata이며 별도 raw 운영 자료를 새로 노출하지 않는다.

검색은 기존 lowercase substring과 함께 공백만 제거한 비교를 병행한다. 따라서 `삼성감사제`와 `삼성 감사제`가 양방향으로 일치하며 fuzzy/semantic/token 검색은 추가하지 않았다.

최종 결과는 focused `46 passed`, 전체 `3,598 passed`, 실패와 skip은 모두 0이다. schema·migration 변경은 없다.

## [시작 상태]

- repository root: `C:\Users\user\Desktop\프로젝트\Q&A 통합\git\qa-auto`
- branch: `main`
- 시작 HEAD: `4abc4bcc03a4a46fddac990e90d9678a1880e85e`
- 시작 working tree: clean
- 기준 commit: `26.8.30 - Negative Learning 관리 UI 및 revoke 연결 완료`
- 운영 DB 및 외부 Naver/DPS/Kakao 작업: 수행하지 않음

## [기존 검색 구조]

기존 흐름은 다음과 같았다.

```text
learning_examples / learning_feedback
  -> manager_rows(limit=2_000)
  -> Positive/Feedback 각각 최근 2,000건 Python list
  -> UI _filter_rows()
  -> UI _paginate_rows()
  -> 현재 page 표시
```

검색 field 자체는 충분했지만 2,000건 밖의 오래된 행은 검색·filter·option 생성 어느 단계에도 들어오지 못했다.

## [2,000건 제한 원인]

- `ui/learning_manager.py`가 두 repository의 `manager_rows(limit=2_000)`을 호출했다.
- 두 `manager_rows()`도 전달 limit을 최대 2,000으로 다시 제한했다.
- 검색과 filter가 SQL이 아니라 반환된 Python list에 적용됐다.
- source/provenance option도 동일 list에서 만들었으므로 오래된 값은 option에서 누락됐다.

최종 구현에서는 활성 Learning Manager 검색 경로의 위 제한이 0곳이며, `manager_rows()`의 고정 최대 2,000 상한도 제거했다. 호환용 `manager_rows(limit=...)`는 명시적으로 요청된 수만 반환하고 Learning Manager UI에서는 사용하지 않는다.

## [Repository 검색 구조 변경]

공통 helper `repositories/learning_manager_query.py`를 추가했다.

- `LearningManagerPage`: `rows`, `total`, `page`, `page_size` 계약
- `manager_search_sql()`: bound parameter 기반 원문/공백 정규화 LIKE predicate
- `manager_page_bounds()`: page size 상한 100, page 범위 보정, offset 계산
- `manager_search_matches()`: 기존 메모리 helper/test 호환 검색

Positive `LearningRepository.manager_page()`와 Feedback `LearningFeedbackRepository.manager_page()`가 각각 다음을 수행한다.

```text
검색어 + filter
  -> WHERE predicate와 bound parameter 생성
  -> COUNT(*)로 정확한 total 계산
  -> page 범위 보정
  -> 기존 stable ORDER BY
  -> LIMIT/OFFSET으로 현재 page만 조회
  -> 현재 page inquiry 상태만 bulk 조회
```

Positive는 `learning_signal_type=POSITIVE`, source, provenance, human verified, validity type/state를 SQL에서 처리한다. Feedback은 source, provenance, human verified, `NEGATIVE/INTENT_CORRECTION/EXCLUDED`를 SQL에서 처리한다.

## [Pagination]

- 기존 page size 20/50/100 유지
- 처음/이전/다음/마지막 UI 유지
- filter가 적용된 `COUNT(*)`를 total로 사용
- total에 따라 repository에서 안전한 현재 page와 offset 계산
- 기존 정렬 유지:
  - `source_created_at`
  - fallback `registered_at`
  - fallback Learning/Feedback `created_at`
  - 동일 시각은 PK `id DESC`
- 결과가 0건이거나 page 범위를 벗어나도 session page를 1 또는 실제 마지막 page로 보정

## [Filter Option]

Positive와 Feedback repository가 각각 `manager_filter_options()`를 제공한다.

- source: `SELECT DISTINCT`
- provenance: 실제 UI precedence를 보존한 `SELECT DISTINCT`
- Python 전체 table materialization 없음
- Positive/Feedback option을 UI에서 합쳐 기존 공통 selectbox 계약 유지

## [검색 Field 보완]

기존 question, canonical learning answer, product name, source, reference, memo, event name, Feedback Reason, provenance 검색은 유지했다.

추가 field:

- `seller_answer`
- `edited_answer`
- `gpt_draft`
- `valid_from`
- `valid_until`
- `condition_json`

`seller_answer`와 `edited_answer`는 기존 관리자 상세 화면에서 이미 표시되는 답변이다. `gpt_draft`는 기존 `learning_examples` 관리 row에 포함된 프로그램 생성 답변이며 raw inquiry export가 아니다. `condition_json`도 동일 상세 화면에서 이미 표시되는 Learning 적용 조건 metadata만 사용한다.

날짜는 DB에 저장된 문자열 표현을 그대로 검색 대상으로 사용하며 별도 parsing 검색 규칙은 추가하지 않았다.

## [검색 정규화]

정규화 범위는 의도적으로 공백류 제거에 한정했다.

- 기존: `strip + lowercase + substring`
- 추가: 원문 비교가 실패하면 query와 대상에서 공백류만 제거하고 substring 비교
- 보존: `삼성`, `배송`, `설치`, `벽걸이` 검색
- 검증: `삼성감사제 -> 삼성 감사제`, `삼성 감사제 -> 삼성감사제`
- 미추가: 형태소 분석, tokenizer, fuzzy, Levenshtein, embedding, semantic search

LIKE wildcard인 `%`, `_`, escape 문자는 literal로 escape한다.

## [성능/전체 로드 여부]

- 검색 없는 기본 목록: count 1회 + 현재 page query 1회
- 검색어 있는 목록: SQL WHERE 검색 + count + 현재 page query
- filter 적용 목록: SQL WHERE filter + count + 현재 page query
- 마지막 page: total 기반 offset의 현재 page만 반환
- source/provenance option: `DISTINCT` query
- Python full-table materialization: 0
- page inquiry 상태: 현재 page ID만 한 번에 bulk 조회
- 명백한 N+1: 없음

schema/FTS/index 추가가 금지된 범위이므로 자유검색은 SQLite의 bound `LIKE` scan을 사용한다. 데이터량 증가 시 DB scan 비용은 있을 수 있으나 전체 row와 본문을 Python으로 전송하는 기존 구조는 제거됐다.

## [SQL 안전성]

- 검색어와 모든 filter 값은 parameter binding 사용
- 사용자 검색어를 SQL 문자열에 interpolation하지 않음
- LIKE wildcard 및 escape 문자 literal 처리
- 동적 SQL 조각은 코드에 고정된 column expression과 whitelist filter만 사용
- 새 table/index/FTS/migration 없음

## [Focused Test]

실행:

```text
python -m pytest -p no:cacheprovider -q
  tests/test_learning_manager_repository_search.py
  tests/test_learning_manager_traceability.py
  tests/test_learning_validity.py
  tests/test_learning_effective_lifecycle.py
  tests/test_learning_feedback.py
```

결과:

- passed: 46
- failed: 0
- skipped: 0
- 실행시간: 60.62초

검증 범위:

- 2,001건 밖 오래된 Positive와 Feedback 검색
- 전체 repository의 source/provenance option
- 정확한 total과 마지막 page
- 기존 stable ordering
- source/provenance/human verified/validity/feedback status filter
- 질문, 학습답변, product, source/reference, event, 날짜, memo, condition, 보조 답변 검색
- 양방향 공백 정규화
- 기존 substring 검색
- 검색/filter/page/상세 선택/수정 후 route context
- validity, effective lifecycle, feedback 회귀

## [전체 Test]

실행:

```text
python -m pytest -p no:cacheprovider -q
```

결과:

- passed: 3,598
- failed: 0
- skipped: 0
- 실행시간: 1,190.60초

테스트는 모두 test fixture, temporary DB, mock을 사용했다. 운영 DB와 외부 서비스는 변경하지 않았다.

## [회귀 검증]

- 오래된 Positive 검색 실패: 0
- 오래된 Feedback 검색 실패: 0
- pagination 오류: 0
- total count 오류: 0
- filter regression: 0
- 기존 substring regression: 0
- context 유지 regression: 0
- Positive/Negative/학습 제외 UI 변경: 없음
- Feedback Reason/revoke 변경: 없음
- TEMPORARY 편집·validity 정책 변경: 없음
- Structured Signal/Historical/Product Facts/Excel Export 변경: 없음

## [잔여 GAP]

- CRITICAL: 0
- HIGH: 0
- MEDIUM: 0
- LOW: 0

Semantic/fuzzy/token 검색은 명시적 범위 밖이며 잔여 GAP으로 분류하지 않는다.

## [최종 판정]

**LEARNING MANAGER SEARCH READY**

**— Learning Manager 검색/관리 개선 최종 완료**

최근 2,000건 제한, metadata/보조 답변 검색 누락, 최소 공백 정규화의 세 GAP이 모두 해소됐다. 현재 schema를 유지하며 전체 repository 검색, 정확한 count/pagination, 전체 filter option, 기존 UI context와 회귀 안전성을 확인했다.
