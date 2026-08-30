# Phase 11-FINAL — Product Facts → Q&A Auto 최종 통합 검증

작업일: 2026-08-30 · 개발 PC offline/read-only 검증 · 외부 등록/전송/서버 작업 없음

## 최종 판정

**PHASE 11 FINAL READY — Product Facts → Q&A Auto 최종 통합 검증 완료**

**PRODUCT FACTS DEVELOPMENT COMPLETE**

Safety 오류, 전체 테스트 실패, DB 변경, CRITICAL backlog가 모두 0이다. 안전하게
UNKNOWN으로 남는 coverage 문제는 종료 blocker로 취급하지 않았다.

## 인계 및 Git 시작 상태

| 항목 | 값 |
|---|---|
| repository root | `C:/Users/user/Desktop/프로젝트/Q&A 통합/git/qa-auto` |
| branch | `main` |
| 시작 HEAD | `9541f8922dc2c040f4a937132029db4d90ac59eb` |
| 시작 working tree | `services/product_knowledge_service.py` 수정, `tests/test_product_fact_subject_scope_11final.py` untracked |
| 시작 staged 변경 | 없음 |
| 사용자 unrelated 변경 | 확인되지 않음. logs/runtime DB/state는 ignored 운영 artifact로 유지 |

Claude가 중단 전에 구현한 weight/VESA subject 경계, component 제외 어법과 신규 테스트를
그대로 인계했다. TEMP scratchpad의 `replay2.json`, `matrix2.json`, routing A/B 결과와
생성 스크립트를 찾아 재사용했고, 이후 코드 변경에 영향받은 harness와 테스트만 다시
실행했다.

## 최종 Product Facts DB

| 항목 | 값 |
|---|---|
| 경로 | `data/product_facts.db` |
| 기대 SHA-256 | `51a9d4a58a28618ad3cd2ea55e43252fe259ab6f930e01d731c2ab3b0afac63b` |
| 시작 SHA-256 | `51a9d4a58a28618ad3cd2ea55e43252fe259ab6f930e01d731c2ab3b0afac63b` |
| `PRAGMA integrity_check` | `ok` |
| `PRAGMA quick_check` | `ok` |
| listing | 94 |
| Git 상태 | `.gitignore`의 `*.db` 규칙으로 ignored, untracked Git artifact 아님 |

모든 DB 접근은 SQLite URI `mode=ro`와 `PRAGMA query_only=ON`으로 수행했다. DB를
stage/commit/push하지 않는다.

## 완료한 production 변경

1. 본체 무게/VESA와 거치대 최대하중/VESA를 질문 subject 기준으로 분리했다.
2. `스탠드 제외`, `스탠드 빼고`, `받침대 미포함`은 구성품 질문이 아니라 본체 질문으로
   처리했다.
3. 반대 방향도 차단하여 일반 본체 무게 질문에 `accessory_max_load_kg`가 제공되지 않게
   했다.
4. G2 중 명백한 설치 표현 `직접 설치`, `혼자 설치`, `혼자서 설치`만 기존
   `installation_method` mapping에 추가했다.
5. `설치 기사 언제`, `내일`, `예정일` 등 날짜/시간 표현은 정적 설치방법 fact를 요청하지
   않고 기존 DPS/order route에 남겼다.

`VERIFIED`만 후보로 사용하며 `NEEDS_REVIEW`, `CONFLICT`, missing fact는 답변 근거로
사용하지 않는 기존 계약은 변경하지 않았다. missing fact는 NO가 아니라 UNKNOWN이다.

## G2 MAPPING_MISSING 최종 검토

Phase 11-H 후보 9건의 원문, exact product, 현재 Phase 11-J canonical value와 ACTIVE
VERIFIED provenance를 대조했다.

| 후보 | 건수 | 판정 | 근거 |
|---|---:|---|---|
| 직접/혼자 설치 | 4 | 수정 | 해당 product에 `installation_method=PROFESSIONAL_TECHNICIAN_REQUIRED`; 전문기사 설치 provenance 존재 |
| SmartThings | 3 | 유지 UNKNOWN | 현재 최종 DB에서 해당 product에 usable `smartthings_hub` evidence 없음 |
| 자동 피벗 | 1 | 유지 UNKNOWN | 해당 product의 `pivot=YES`는 일반 피벗이며 자동 피벗과 semantic 불일치 |
| 일반 보증기간 | 1 | 유지 UNKNOWN | `pixel_defect_warranty_period_months`는 불량화소 한정으로 일반 보증기간과 semantic 불일치 |

검토 9건, 실제 개선 inquiry 4건, 신규 synonym mapping 3개다. 새 ontology, 새 DB field,
fuzzy expansion, compatibility 추론은 추가하지 않았다.

## 실제 Inquiry Replay

`data/oje_automation.db`의 실제 문의를 read-only로 재생했다. 전체 corpus는 Phase 11-H와
같은 2,772건이다. 넓은 의미 기반 product-fact cohort는 786건이다. Phase 11-H의 789건보다
3건 적은 이유는 이번 최종 수정으로 `설치 기사 언제`류 순수 일정 문의를 Product Facts
분모에서 제외하고 DPS/order 영역으로 명확히 되돌렸기 때문이다. 임의 데이터는 추가하지
않았다.

| 항목 | 값 |
|---|---:|
| total inquiry | 2,772 |
| product-fact inquiry | 786 |
| product-fact exact join | 261 |
| 전체 product_id exact join | 651 |
| evidence-used inquiry | 67 |
| evidence coverage | 67 / 261 = **25.7%** |
| safe auto candidate | 65 |
| DPS-required | 982 |
| order-required | 1,283 |
| Missing Item | 22 |
| generation blocked | 30 |

G2 4건이 safe candidate에 새로 포함됐고, 순수 설치 일정 4건의 정적
`installation_method` 노출이 제거되어 evidence-used 총수는 이전 67과 같다.

## Safety 결과

| 지표 | 결과 |
|---|---:|
| cross-product leakage | **0** |
| unsupported claim | **0** |
| DPS bypass | **0** |
| Missing Item wrong auto-answer | **0** |
| failed-listing unsafe fact | **0** |
| NEEDS_REVIEW unsafe use | **0** |
| CONFLICT unsafe use | **0** |
| component inheritance error | **0** |
| manufacturer semantic error | **0** |
| weight semantic error | **0** |
| VESA semantic error | **0** |
| set-top inclusion/compatibility confusion | **0** |
| unsupported negative | **0** |

retrieval-only matrix는 `스탠드가 빠졌어요`, `벽걸이 부품이 없어요` 3개에서 static fact를
관찰했지만, 실제 pipeline은 세 건 모두 `is_missing_item_report=True` 및
`can_generate_answer=False`로 차단했다. 따라서 Missing Item wrong auto-answer는 0이며
기존 staff-review route가 유지된다.

DPS-required 문의 중 2건은 복합 질문의 정적 fact도 함께 조회됐지만
`requires_dps_lookup=True`가 유지되어 bypass는 아니다. Product Facts enabled/disabled
2,772건 A/B에서 `requires_dps_lookup`, `requires_order_id`, generation/manual/delivery
routing 차이는 **0건**이었다.

KT/SK/딜라이브/기존 셋톱박스 연결 질문 4종은 모든 94개 상품에서 Product Facts 제공이
없어 UNKNOWN을 유지했다. 거치대 최대 지원 무게와 제품 자체 무게, 거치대 VESA와 제품
자체 VESA의 교차 제공도 0이었다.

## 성능

현재 최종 DB, 현재 working tree에서 answer-path retrieval 144회를 측정했다.

| n | median | p95 | max |
|---:|---:|---:|---:|
| 144 | 3.60 ms | 5.43 ms | 10.28 ms |

외부 GPT, DPS, network latency는 포함하지 않았다.

## 테스트

| 구분 | 결과 |
|---|---|
| focused | **236 passed / 0 failed / 0 skipped**, 64.03s |
| 전체 suite | **3,592 passed / 0 failed / 0 skipped**, 1,135.22s |

focused 범위는 신규 Phase 11-FINAL subject/G2 tests, Phase 11-G mapping, Product Facts
guard/Safety Gate, Missing Item, delivery/DPS routing이다. 테스트 삭제, skip, xfail로 결과를
우회하지 않았다.

## Backlog

아래 숫자는 inquiry 수가 아니라 잔여 이슈 **범주 수**다.

| severity | 수 | 내용 |
|---|---:|---|
| CRITICAL | **0** | 없음 |
| HIGH | **0** | 없음 |
| MEDIUM | **3** | G1 FACT_MISSING, G3 FACT_NOT_VERIFIED, G6 ONTOLOGY_GAP |
| LOW | **2** | product_id 부재 coverage, 이번에 안전상 거절한 G2 표현군의 UNKNOWN 유지 |

G1/G3/G6, 낮은 evidence coverage, 외부 셋톱박스 호환 UNKNOWN은 안전한 미답변이며 이번
종료의 blocker가 아니다. 이 검증에서 추가 개발하지 않았다.

## 최종 체크리스트

| 조건 | 결과 |
|---|---|
| Safety 지표 전부 0 | 충족 |
| 전체 tests failed 0 | 충족 |
| DB integrity | `ok` |
| 종료 DB SHA | `51a9d4a58a28618ad3cd2ea55e43252fe259ab6f930e01d731c2ab3b0afac63b` |
| DB SHA unchanged | 충족 |
| CRITICAL backlog 0 | 충족 |
| DB/개인정보/운영 artifact stage 제외 | 충족 |

조건을 모두 충족하므로 Product Facts 개발을 종료한다.
