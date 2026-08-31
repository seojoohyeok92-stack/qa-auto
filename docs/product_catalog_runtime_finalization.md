# 상품정보 JSON 전환 및 운영 KPI 최종 정리

## 요약

상품 사양의 기본 runtime 근거를 `data/model_data_with_color.json`으로 전환했다.
기본 `ProductKnowledgeService` 경로는 더 이상 `product_facts.db` 또는
`ProductFactRepository`를 생성하거나 읽지 않는다. 모델을 안전하게 하나로 식별하지
못하거나 JSON에 질문에 필요한 사실이 없으면 `UNKNOWN`/직원 검토를 유지한다.

Dashboard 기본 KPI는 자동 등록률, 직원 수정률, 직원 검토 필요율 세 개로 정리했고,
자동 답변 생성률은 상세 분석의 진단 지표로 이동했다. 직원 수정률은 내부 수정과
`NAVER_CORRECTION_APPLIED`를 포함하되, 수정 여부가 확인되지 않은 과거 답변을
무수정으로 계산하지 않는다.

또한 명백한 질문-답변 핵심 주제 누락은 `Answer Coverage`에서 직원 검토로 전환해
RULE의 keyword overlap만으로 관련 없는 답변이 자동 등록되는 경로를 차단했다.
Naver POST 성공 시에는 실제 검증·전송된 payload만 별도의 멱등 Kakao 성공 알림에
표시한다. 초안, 이미 답변됨, 등록 확인 불가 상태에서는 계속 `답변: -`이다.

## 시작 상태

- 시작 branch: `main`
- 시작 HEAD: `93e59d36667ecb1fd4e20212c5a42180efa59322`
- 개발 DB: `data/oje_automation.db`를 읽기 전용으로 사용했다.
- 외부 Naver/Kakao/DPS/OpenAI 호출은 수행하지 않았다.

## JSON 카탈로그

- 위치: `data/model_data_with_color.json`
- 파일 크기: 1,193,566 bytes
- 모델: `MODEL_CATALOG` 1,586건, alias 72건
- canonical key 중 중복 모델: 15개 key group / 중복 entry 20건. variant는 key와
  record의 모델·색상·옵션 정보를 유지한다.
- JSON에는 `model`, `brand`, `color`, `size_inch`, `resolution`, `hz`, `spec`,
  `speaker`, `vesa`, `weight`, `stand`, `bracket` 등 상품 Q&A용 필드가 있다.
- 모델 식별은 명시 모델코드 exact → normalized exact → 상품명/옵션 안의 충분히 긴
  모델키 → 명시 alias 순서다. 후보가 0개 또는 2개 이상이면 다른 모델을 빌리지 않고
  `PRODUCT_CATALOG_MODEL_NOT_FOUND` 또는 `PRODUCT_CATALOG_AMBIGUOUS`를 반환한다.
- JSON 값은 literal evidence만 prompt에 전달한다. RF, HDMI, LAN, USB, Wi-Fi,
  Bluetooth, VESA, 무게, 크기, 스탠드 간격은 질문 주제와 직접 맞는 값만 제공하며,
  스탠드 지원 무게와 본체 무게를 혼용하지 않는다.

`answer_data/learning/model_data_with_color.json`은 동일 blob인 기존 정적 복제본으로
남아 있으나, 기본 runtime source-of-truth는 `data/` 파일 하나다. 두 파일은 Git
추적 중이고 1.2MB이며 민감정보나 DB artifact가 아니므로 일반 Git 관리 범위다.

## Product Facts 분리

기존 기본 생성 경로는 `AnswerService` → `ProductKnowledgeService()`였고,
후자는 기본 `ProductFactRepository`를 생성할 수 있었다. 변경 후 기본 서비스는
`ProductCatalogRepository`만 생성한다. 따라서 runtime 기본 경로의
`product_facts.db` read 및 `ProductFactRepository` 호출은 0이다.

- runtime 상품정보 조회: JSON catalog
- 상품 사양 safety/provenance: JSON literal evidence의 field/scope 검증
- legacy/diagnostic/test: `ProductFactRepository`, Product Facts repository, 과거
  matrix/export/test 코드. 이들은 자동 답변 기본 경로에 연결되지 않는다.

객관 사양은 JSON 명시값을 우선한다. JSON에 없는 사실은 Learning으로 자동 반박하지
않고, 기간성 정책은 Learning/Template, 주문·배송·설치는 Order/DPS를 계속 우선한다.
명백한 충돌은 자동 등록하지 않고 직원 검토로 보낸다.

## Dashboard KPI 정의와 원시값 검증

KST `source_created_at`(없으면 `created_at`) 기준 실제 유입 unique inquiry를
자동 등록률/직원 검토 필요율의 cohort로 사용한다. 재동기화는 새 유입으로 세지 않는다.

| KPI | 분자 | 분모 | 최근 7일 | 최근 30일 | 판정 |
| --- | --- | --- | ---: | ---: | --- |
| 자동 등록률 | `naver_post_attempts`의 `POSTED`이며 auto post run이 있는 unique inquiry | 실제 유입 unique inquiry | 13 / 122 = 10.7% | 15 / 847 = 1.8% | REDEFINED |
| 직원 수정률 | 내부 수정 또는 `NAVER_CORRECTION_APPLIED` unique inquiry | 수정 여부가 확인된 답변 cohort | 0 / 0 = 측정 데이터 부족 | 3 / 3 = 100.0% | INSUFFICIENT_DATA/KEEP |
| 직원 검토 필요율 | 기간 중 review/block event가 있는 unique inquiry | 실제 유입 unique inquiry | 44 / 122 = 36.1% | 71 / 847 = 8.4% | REDEFINED |

기존 0.0% 직원 수정률은 실제 무수정 0건이라는 뜻이 아니라, 과거 행에
`REVIEWED_NO_CHANGE` 또는 수정 provenance가 없어 측정 가능한 분모가 없었던 경우도
0으로 보이던 표시 문제였다. 이제 분모가 0이면 `측정 데이터 부족`으로 표시한다.
Dashboard와 raw DB는 동일 repository aggregation을 사용하므로 집계 mismatch는 0이다.

`answer_versions.version_kind='NAVER_CORRECTION_APPLIED'`은 직원 수정률에 포함한다.
내부 수정과 같은 inquiry에 함께 있어도 `DISTINCT inquiry_id`로 한 번만 센다. 이 신호는
Positive/Negative Learning 생성 trigger가 아니다. 명시적인 관리자 Positive 또는
Negative/학습 제외만 기존 Learning 저장 경로를 사용하며, 수동 Naver 등록도 자동
Learning을 만들지 않는다.

## 질문 이해, RULE 및 Answer Coverage

`AnswerEngine`은 행사명이 문맥일 뿐 실제 질문 목적의 증거가 아니라는 원칙을 적용한다.
행사 신청 과정의 주문번호 식별·보완 질문에 행사 일반 안내 RULE을 fallback으로 쓰지
않는다. 실제 주문내역 근거가 없으면 자동 답변하지 않는다.

`SemanticCoverageService`는 최종 답변의 핵심 topic과 sub-question을 기록한다.
명백한 `FAIL`/`PARTIAL`은 `AnswerService`에서 `NEEDS_REVIEW` 및 auto-post 불가로
전환한다. `UNKNOWN`은 관찰만 하므로 정상 단일 질문을 과도하게 막지 않는다.
구매한 다른 상품/나머지 상품/같이 주문한 상품 모델명은 JSON에서 유사 모델을 고르는
사양 질문이 아니라 order evidence가 필요한 질문으로 분류한다.

개발 DB에는 운영 문의 `325407138`, `687057794`, `687051098`이 없었다. 따라서 과거
event sequence/provenance는 개발 PC에서 확인 불가이며, 다음은 실제 원문 기반
synthetic production-logic regression으로 검증한다.

- `325407138` 유형: 행사 context + SH 주문번호 질문은 주문번호 핵심질문을 보존하고
  온누리 환급 일반 RULE을 자동 선택하지 않는다. coverage incomplete/근거부족이면
  auto-post 불가다.
- `687057794` 유형: 스탠드 작성 여부 + 실제 구매한 다른 제품 모델명은 두 핵심
  sub-question이다. 스탠드 RULE 하나만으로 전체 PASS할 수 없고, 구매 evidence 없이
  다른 모델을 추론하지 않는다.
- `687051098` 유형: 개발 DB 원본과 운영 historical provenance는 없다. 유선/무선
  배터리 관련 과거 답변은 JSON literal이 아닌 기존 RULE source의 조건부 안내였으며,
  JSON 근거가 없으면 새 경로에서 이를 상품 사양으로 창작하지 않는다.

추가 GPT 호출은 0이다. 기존 deterministic semantic/sub-question 정보와 coverage
anchor를 재사용했다.

## Kakao 등록 답변 표시

초안 생성 알림은 실제 Naver 등록의 증거가 아니므로 `답변: -`를 유지한다. confirmed
`NaverPostService.post()` 성공 경로에서만 Naver가 승인한 정확한 `request.final_answer`
를 `action='posted'`로 outbox에 넣는다. notify key는
`naver-posted:{inquiry_id}:{attempt_id}`여서 동일 성공 알림 retry는 중복되지 않는다.

- verified POST success + verified text: 실제 답변 표시
- seller-answer sync로 verified text를 확보한 별도 등록완료 알림 경로: 실제 답변 표시 가능
- draft-only, already-answered/duplicate skip, POST failure, 확인 불가: `답변: -`

따라서 `687051098`의 과거 `답변: -` 발생 순서는 운영 DB 부재로 확정할 수 없지만,
현재 코드에서 생성 알림만 있었고 POST 성공 알림이 없던 일반 경로가 재현 가능한
원인이었다. 성공 payload를 등록 답변인 것처럼 추측하지 않고, 실제 POST success에서만
표시하도록 수정했다.

## 전체 답변 품질 Audit

개발 DB 전체 2,772건(`PRODUCT_INQUIRY` 1,662건, `CUSTOMER_INQUIRY` 1,110건)을
read-only로 조사했다. 외부 Provider/Naver/DPS를 재호출하지 않기 위해, active draft가
존재하는 367건을 현재 deterministic `SemanticCoverageService`에 다시 적용했다.
이 중 RULE 219건, GPT 144건, source 미기록 4건이며, `GENERATED` 151건,
`NEEDS_REVIEW` 213건, `NOT_SUPPORTED` 3건이다. posted history가 있는 active draft는
15건이다. Template은 별도 source로 저장된 active draft가 없어 cohort 0건이다.

| Audit 결과(저장된 과거 출력) | 건수 | 수정 후 처리 |
| --- | ---: | --- |
| coverage PASS | 14 | 기존 처리 보존 |
| coverage UNKNOWN | 345 | `MEASUREMENT_LIMITATION`: deterministic anchor로 의미 판단 불가, 기존 다른 safety gate 유지 |
| `ANSWER_INCOMPLETE` | 8 | 이후 생성에서는 `NEEDS_REVIEW`, auto-post 불가 |
| `ANSWER_SEMANTIC_MISMATCH` | 5 | 이후 생성에서는 `NEEDS_REVIEW`, auto-post 불가 |
| `RULE_PARTIAL_COVERAGE` | 7 | RULE도 동일 coverage gate 적용 |
| 과거 `VALIDATOR_FALSE_PASS` 후보 | 8 | coverage gate가 Validator 전 auto-post 경로 차단 |
| 과거 posted 상태의 coverage 실패 후보 | 5 | 과거 등록 이력은 보존, 신규/재생성에서는 auto-post 불가 |

이 표의 8/5/7/5는 **수정 전 저장 답변의 historical audit**이며, 이미 등록된 답변을
되돌리거나 운영 DB를 변경한 수치가 아니다. 변경 후 production path에서 명백한
`FAIL`/`PARTIAL`은 저장 전에 직원 검토로 전환되므로 해당 신규 `UNSAFE_AUTO_POST`는
0이어야 한다. regression test는 행사 keyword 오선택, 복합질문 일부 답변, 구매상품
추론, 정상 행사 RULE을 production logic 단위로 고정한다.

다음 taxonomy는 저장된 결과와 schema만으로 신뢰성 있게 자동 판정할 수 없어
`MEASUREMENT_LIMITATION`이다: `INTENT_MISUNDERSTOOD`, `WRONG_RULE`,
`UNNECESSARY_ORDER_LOOKUP`, `MISSING_ORDER_LOOKUP`, `UNNECESSARY_DPS_LOOKUP`,
`MISSING_DPS_LOOKUP`, `JSON_EVIDENCE_MISSED`, `JSON_MODEL_MISMATCH`,
`UNSUPPORTED_FACT`, `FALSE_INFORMATION_INSUFFICIENT`, `VALIDATOR_FALSE_BLOCK`.
각 항목은 현재 DB에 사람의 정답 intent/evidence label이 없기 때문에 0으로
위장하지 않았다. 개발 DB에서 재현 가능한 대표 위험은 fixture와 coverage gate로
막고, 모델 식별 불가/JSON 값 부재는 계속 UNKNOWN·직원 검토로 보수 처리한다.

## 성능 및 회귀 검증

카탈로그는 파일의 mtime/size 기반 process cache와 normalized model index를 사용한다.
개발 PC 측정: cold load 15.825ms, lookup median 0.2925ms, p95 0.6022ms, max 1.019ms
(1,002회 샘플). 문의마다 JSON 전체를 재로딩하거나 Python 전체 DB materialization을
하지 않는다.

Focused regression은 JSON catalog exact/ambiguous/missing, RF 및 스탠드 scope,
KPI cohort/수정률, Answer Coverage, 행사 RULE, 구매상품 추론 차단, Naver POST/Kakao
verified answer 및 retry contract를 포함한다.

최종 전체 회귀는 2026-09-01에 `%TEMP%\\qa_auto_full_final_20260901.log`로
stdout/stderr와 종료 코드를 보존하여 실행했다.

- `3648 passed`
- `0 failed`
- `0 skipped`
- `PYTEST_EXIT_CODE=0`
- 실행시간: 1,359.33초(22분 39초)

stale pytest cache 후보도 실제 파일 단위 재현으로 확인했다. 자동 Learning 관련 5개
파일은 `60 passed`, 나머지 cache 파일군은 `830 passed`였으며 모두 exit code 0이다.
따라서 cache의 과거 실패 표시는 현재 실패나 production regression의 근거가 아니다.

## 잔여 한계와 최종 판정

- 운영 DB가 개발 PC에 없으므로 세 운영 사례의 과거 실제 event sequence는 확인 불가다.
  이는 safety blocker가 아니며 production logic synthetic regression으로 보완했다.
- JSON에 없는 사실은 UNKNOWN/직원 검토이며, coverage를 높이기 위해 추론하지 않는다.
- Product Facts legacy 코드와 DB artifact는 삭제하지 않았지만 runtime 기본 dependency는
  끊었다.

### 최종 위험 판정

CRITICAL은 없다. 다만 HIGH 1건이 남아 있다. 현재 `InquiryAnalysisService`의
deterministic first-pass와 `AnswerEngine`의 keyword RULE 선택은 여전히 독립적인
판단 계층이며, GPT/Semantic 분석 결과가 모든 RULE·order·DPS 결정보다 항상 먼저
우선하도록 단일 계약으로 통합되지는 않았다. 이번 변경의 `Answer Coverage`는
명백한 주제 누락/일부 답변을 auto-post 전에 review로 전환하지만, 최초 RULE 선택이
semantic 결과를 덮어쓰는 가능한 모든 경로를 제거했다는 전수 근거는 아니다.

따라서 전체 회귀는 성공했지만, 실제 운영에서 반복 관찰된 keyword-only routing
위험을 숨기지 않고 HIGH로 유지한다. 이 상태에서는 `READY`, commit, push를
선언하지 않는다.
