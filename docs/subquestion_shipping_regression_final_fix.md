# Q&A Auto 줄바꿈 및 배송/A/S 답변 회귀 최종 수정

## [요약]

- 시작 기준은 `main`의 `b77db7492ed56a02b818ee2d8f39c82f9bdf027a`이다.
- 일반 prose 줄바꿈을 모두 독립 질문으로 취급하던 `split_subquestions()`를 보수적으로 수정했다. 설명 문맥과 하나의 최종 질문은 결합하고, 명시적 복수 질문과 번호 목록은 계속 분리한다.
- `받을 수`, `기간` 같은 광범위 표현만으로 배송 정책이 선택되지 않도록 배송 문맥과 A/S·수리·보증 문맥을 구분했다.
- 명시적 A/S 질문에 배송 안내만 생성되는 경우 Template Validator가 `PASS`하지 않도록 deterministic semantic alignment 방어를 추가했다.
- 외부 Provider 호출, DB/schema/migration, Learning 데이터 변경은 없다.
- focused tests는 `548 passed / 0 failed`, 전체 suite는 `3618 passed / 0 failed / 0 skipped`이다.

## [시작 상태]

- repository: `C:\Users\user\Desktop\프로젝트\Q&A 통합\git\qa-auto`
- branch: `main`
- HEAD: `b77db7492ed56a02b818ee2d8f39c82f9bdf027a`
- 시작 working tree: clean
- 사용자 unrelated 변경: 확인되지 않음

## [CASE A 원인]

기존 `answer/text_utils.py::split_subquestions()`는 일반 prose의 newline을 질문 경계로 사용했다. 그 결과 삼성 페스티벌 구매처 문의처럼 앞 문장이 신청·보완요청 문맥이고 마지막 문장이 실제 질문인 단일 의미 문의가 여러 불완전 조각으로 분리됐다. 분리된 조각은 구매처 관련 핵심 의미를 충분히 유지하지 못해 이미 존재하는 구매처 Learning이 retrieval되지 않았다.

## [CASE B 원인]

`services/inquiry_analysis_service.py`와 `answer/engine.py`의 배송 판정은 `받을수`, `받을 수`, `기간` 같은 일반 표현을 문맥 없이 배송 신호로 사용할 수 있었다. 따라서 `AS 받을수`, `A/S 기간`, `무상수리기간`, `무상점검 받을수`가 `FIXED_POLICY_SHIPPING`으로 오선택됐다. 기존 Template Validator는 질문-답변 domain alignment를 검사하지 않아 이 배송 답변을 형식상 `PASS`할 수 있었다.

## [Splitter 수정]

- `answer/text_utils.py`에 prose newline 결합 helper를 추가했다.
- 질문형 종결과 명시적 `?`를 보수적으로 식별한다.
- 신뢰할 수 있는 질문 줄이 하나이면 앞의 설명 문맥과 결합해 하나의 질문으로 유지한다.
- 질문 줄이 여러 개이면 각 질문을 분리하되, 앞선 설명 문맥을 첫 질문에 보존한다.
- 번호 목록은 기존 wrapped-numbered-list 경로를 그대로 사용한다.
- 질문 경계가 불명확한 입력은 공격적으로 합치지 않고 기존 newline 경계를 보존한다.
- title/content 중복 제거, filler 제거, atomic sub-question 연결 계약은 유지했다.

## [배송 Context 수정]

`answer/text_utils.py`의 공통 deterministic helper를 `services/inquiry_analysis_service.py`와 `answer/engine.py`가 함께 사용하도록 했다.

- 명시적 배송 문맥: `배송`, `택배`, `도착`, `출고`, `수령`, `받는 데`, `상품을 ... 받을 수` 등
- 명시적 A/S 문맥: `AS`, `A/S`, `수리`, `고장`, `무상수리`, `서비스센터`, `점검`, 화면·영상 멈춤/미출력 등
- 비배송 서비스 문맥: A/S·수리·점검·교환·환불·반품 문맥의 `받을 수`, 보증·수리·교환·반품 `기간`

명시적 비배송 서비스 문맥이 있고 배송 문맥이 없으면 배송 intent 및 fixed shipping rule을 차단한다. 반면 `언제 받을 수 있나요`, `배송기간`, `택배 배송은 며칠 걸리나요` 등 정상 배송 문의는 유지한다. 고장 설명과 별도로 새 상품 도착일을 묻는 것처럼 배송 문맥이 명시된 경우도 배송을 유지한다.

## [A/S Routing]

`services/inquiry_analysis_service.py`에서 A/S 신호를 product-general 분류에 포함했다. 이에 따라 명시적 A/S 문의는 광범위 배송 keyword에 선점되지 않고 기존 Learning/GPT 경로로 진행할 수 있다. 새로운 taxonomy나 Learning은 추가하지 않았다.

확정 사례 4건 유형은 모두 다음과 같이 변경됐다.

- before: `FIXED_POLICY_SHIPPING` 4건
- after: `FIXED_POLICY_SHIPPING` 0건, fixed rule `NO_MATCH` 4건

## [Whole-question Fallback]

추가 fallback은 구현하지 않았다. 수정된 splitter가 페스티벌 prose를 하나의 완전한 retrieval query로 유지하고 기존 구매처 Learning을 정상 검색하는 것을 회귀 테스트로 확인했다. 별도의 whole-question fallback은 복합문의에서 무관한 evidence 범위를 넓힐 수 있으므로 이번 최소 수정에는 필요하지 않다고 판정했다.

## [Validator Alignment]

`answer/answer_validator.py`의 Template 경로에 질문을 전달하고, 다음 고신뢰 mismatch만 차단했다.

> 명시적 A/S·고장 질문 + 배송기간/출고/도착 안내만 있는 답변

이 경우 `TEMPLATE_SEMANTIC_ALIGNMENT` 검사가 실패한다. A/S 질문에 정상 A/S 답변, 배송 질문에 정상 배송 답변은 기존대로 통과한다. `services/answer_service.py`에서도 fixed template의 사전 검사와 최종 검사가 질문을 전달하므로 우회 경로가 없다.

또한 `services/auto_processing_eligibility_service.py`는 실제 validator 결과가 `REVIEW_REQUIRED`이거나 review signal을 포함하면 `VALIDATOR_REVIEW_REQUIRED`로 독립 차단한다. 기존 final auto-post 안전장치를 약화하지 않았다.

## [Corpus Before/After]

개발 DB의 기존 `PRODUCT_INQUIRY` 1,662건을 SQLite read-only URI로 deterministic replay했다. baseline은 시작 HEAD의 코드를 메모리에서 로드해 비교했으며 DB에는 write하지 않았다.

| 지표 | Before | After |
|---|---:|---:|
| 전체 replay | 1,662 | 1,662 |
| runtime newline 문의 | 663 | 663 |
| split 2개 이상 | 662 | 477 |
| 오분리 위험 후보 | 349 | 188 |
| 확정 A/S→배송 오선택 | 4 | 0 |
| 명확한 복수질문 false merge | - | 0 |
| 번호 목록 false merge | - | 0 |
| 정상 배송 regression | - | 0 |

기존 진단 유형은 `WRAPPED_SINGLE_QUESTION 350`, `TRUE_MULTI_QUESTION 76`, `NUMBERED_MULTI_QUESTION 32`, `MIXED 83`, `AMBIGUOUS 122`였다. 이번 보수적 변경은 명확한 복수질문 집합을 단일 질문으로 합치지 않았다.

## [False Merge]

`TRUE_MULTI_QUESTION`, `NUMBERED_MULTI_QUESTION`, `MIXED`를 집중 비교한 결과 명확한 복수질문이 하나로 합쳐진 사례는 0건이었다. 번호 marker가 손실된 사례도 0건이었다. 질문 경계가 모호한 corpus는 과잉 결합보다 기존 분리를 유지하도록 설계했다.

## [정상 배송 Regression]

실제 corpus에서 rule 변화는 7건이었으며 모두 A/S·고장·점검·보증과 광범위 배송 keyword가 충돌한 사례였다. 명시적 배송 문맥을 가진 정상 배송 문의의 route regression은 0건이었다. 별도 외부 Provider 호출이나 GPT understanding 단계는 추가하지 않았다.

## [Focused Tests]

- 결과: `548 passed / 0 failed`
- 검증 범위: prose wrapping, 명시적 복합질문, 번호 목록, 설명+복수질문, atomic analysis, 확정 A/S 4종, 정상 배송 표현, Learning retrieval, Template Validator alignment, answer service integration, auto-post eligibility 및 기존 관련 회귀 테스트
- 페스티벌 구매처 fixture는 기존 Learning helper를 사용했으며 새 Learning을 추가하지 않았다.

## [전체 Tests]

- 결과: `3618 passed / 0 failed / 0 skipped`
- 실행 시간: 약 19분 14초
- 현재 working tree 기준 전체 Q&A Auto test suite 성공

## [Safety]

- 확정 A/S→배송 오선택: 0
- A/S 질문 + 배송-only 답변 Validator `PASS`: 0
- 정상 배송 route regression: 0
- 명확한 복수질문 false merge: 0
- 번호 목록 false merge: 0
- auto-post safety regression: 0
- 실제 auto-post/Naver/DPS/Kakao 호출: 0
- 외부 Provider 호출 추가: 0
- DB/schema/migration 변경: 0
- Product Facts 및 Learning 데이터 변경: 0

## [잔여 Backlog]

- CRITICAL: 0
- HIGH: 0
- MEDIUM: 0
- LOW: 1

LOW 1건은 보수적 heuristic이 그대로 분리한 188개의 모호한 위험 후보에 대한 장기 corpus 관찰 항목이다. 이 수치는 확정 오류 수가 아니라 기존 진단 heuristic 후보이며, 현재 요구 범위의 명확한 사례와 Safety 조건을 막지 않는다. Semantic Search나 새 Learning Phase로 확장하지 않는다.

## [최종 판정]

**Q&A ANSWER REGRESSION FIX READY**

**— 줄바꿈 및 배송/A/S 답변 회귀 최종 수정 완료**

READY 조건을 모두 충족했다. 서버 작업은 수행하지 않았으며 개발 PC의 수정·테스트·read-only replay까지만 완료했다.
