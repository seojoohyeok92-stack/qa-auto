# Phase 11-F — Product Facts 실제 Q&A 통합 최종 검증

작업일: 2026-08-30 · production 코드 수정 없음 · DB 수정 없음 · 서버 PC 미접속

---

## 요약

실제 최종 Product Facts DB(`e0cdd363…`)를 기본 경로로 사용하는 상태에서,
문의 → 라우팅 → Product Facts 조회 → evidence/prompt → validator → auto-post gate까지
통합 검증했습니다.

| 안전 지표 | 결과 |
|---|---|
| cross-product leakage | **0** |
| unsupported claim | **0** |
| DPS bypass | **0** |
| Missing Item 잘못된 auto-answer | **0** |
| unsafe `COLLECTION_FAILED` fact 사용 | **0** |
| NEEDS_REVIEW / CONFLICT fact의 unsafe 사용 | **0** |
| component inheritance 오류 | **0** |
| brand/manufacturer semantic 오류 | **0** |
| **False Positive** | **0** |
| **False Negative (safety gate 과차단)** | **0** |

안전성 실패는 한 건도 없습니다. 다만 **topic 매핑 공백 12건**이 확인되어
사용 가능한 VERIFIED fact가 질문에 도달하지 못하는 경우가 있습니다(오답이 아니라 미답변).

---

## Git 시작 상태

| 항목 | 값 |
|---|---|
| branch | `main` |
| HEAD | `a8edf0c` |
| origin/main | `a8edf0c` (동일) |
| `git status` | **clean** |

기존 working tree 변경이 없었으므로 이번 Phase와 분리할 사용자 변경이 없습니다.

---

## DB Identity

| 항목 | 값 |
|---|---|
| 경로 | `data/product_facts.db` |
| size | 142,131,200 |
| mtime | 2026-08-29 23:42:01 |
| SHA-256 | `e0cdd3639cb4f0c5f9bc3f2d1f3c4caf020deca45b9144590614e4bc6f55a078` **일치** |
| `PRAGMA integrity_check` | **ok** |
| listings | 94 |
| collection_status | `COLLECTION_SUCCESS` 93 / `COLLECTION_FAILED` 1 |
| `COLLECTION_FAILED` listing | `13074225226` |

DB write는 하지 않았습니다.

---

## Runtime 기본 DB 경로

경로 주입 없이 production 기본 부트스트랩으로 확인했습니다.

| 항목 | 값 |
|---|---|
| 환경변수 `OJE_PRODUCT_FACTS_DB_PATH` | `None` |
| `.env` 내 해당 키 | **0건** |
| `get_product_facts_path()` | `data\product_facts.db` |
| `ProductFactRepository().identity()` sha256 | `e0cdd363…6f55a078` **일치** |

구 DB나 다른 경로를 읽는 상태가 아닙니다.

---

## Production Answer Path

`a8edf0c` 코드에서 직접 확인한 경로입니다.

```
InquiryAnalysisService.analyze()           ← intent / routing / can_generate_answer
InquiryProcessingPlanService.create()      ← requires_order_lookup / requires_dps_lookup 확정
        │  (answer_service.py 1215행)
        ▼
ProductKnowledgeService.facts_for_inquiry() (answer_service.py 1236행)
        │   입력: product_id, question, sub-questions, model_code
        │   출력: ProductKnowledgeResult(safe_facts, excluded_facts,
        │         collection_status, component_subject, unavailable_reason)
        │   중앙 exclusion: _judge() → _exclusion_reason()
        ▼
request.metadata["product_knowledge"]
        ▼
HybridAnswerService._product_facts_context()  ← prompt의 PRODUCT_FACTS 블록
        ▼
AnswerValidator                                ← 같은 fact로 grounding 검증
        ▼
AutoProcessingEligibilityService               ← PRODUCT_FACT_NOT_VERIFIED 차단
        ▼
AutoPostPipelineService
```

fail-closed는 전부 `_exclusion_reason()` 한 곳에서 결정됩니다
(`SUPERSEDED_BY_LATER_RUN`, `VERIFICATION_*`, `RESOLUTION_*`, `VOLATILE_LISTING_FACT`,
`COLLECTION_STATUS_NOT_CURRENT`, `NO_SELECTED_VALUE`, `VALUE_EMPTY_OR_UNKNOWN`,
`NO_ACTIVE_PROVENANCE`, `PROVENANCE_NOT_VERIFIED`, `MODEL_SCOPE_MISMATCH`,
`COMPONENT_SUBJECT_UNRESOLVED`, `PRODUCT_LINE_NOT_IN_VALUE`).

---

## Routing 선행 계약

**실측으로 증명했습니다.** 실제 문의 400건을 두 조건으로 분석했습니다.

| 조건 | 방법 |
|---|---|
| A | Product Facts 정상 (기본 경로) |
| B | `OJE_PRODUCT_FACTS_DB_PATH`를 없는 경로로 지정해 완전 비활성 |

비교 대상: `can_generate_answer`, `requires_dps_lookup`, `requires_order_id`,
`manual_review_required`, `delivery_question`.

```
비교 문의 수      : 400
라우팅 결정 차이  : 0
```

Product Facts가 아예 조회되지 않아도 라우팅이 **한 건도** 달라지지 않습니다.
역방향 feedback 경로가 없다는 구조적 사실이 데이터로 확인되었습니다.

분포: `requires_dps_lookup=True` 103건, `can_generate_answer=False` 5건, 정상 292건.

---

## Product Facts Safety Contract

| 조건 | 검증 |
|---|---|
| ACTIVE + VERIFIED + resolution 정상 | positive 34건 전부 충족, 위반 0 |
| provenance 존재 | 숫자 fact 21건 표본에서 provenance 누락 **0** |
| NEEDS_REVIEW 사용 금지 | 15건 표본, 제공 0 / prompt 유입 0 |
| CONFLICT 사용 금지 | 15건 표본, 제공 0 / prompt 유입 0 |
| 없는 fact → UNKNOWN | UNKNOWN 20건, 부정 단어 유입 **0** |

UNKNOWN 시나리오에서 `없습니다` / `미지원` / `지원하지` / `미포함` / `불가능` /
`not supported`가 evidence·prompt에 등장한 사례는 **0건**입니다.

---

## COLLECTION_FAILED 검증

`13074225226`을 기본 경로에서 조회했습니다.

| volatility | 판정 |
|---|---|
| `DYNAMIC_LISTING_FACT` 8건 | 전부 `VOLATILE_LISTING_FACT` |
| `SEMI_STATIC_POLICY_FACT` 9건 | 전부 **`COLLECTION_STATUS_NOT_CURRENT`** |
| `STATIC_PRODUCT_FACT` | USABLE 18 / `SUPERSEDED_BY_LATER_RUN` 36 / `VERIFICATION_NEEDS_REVIEW` 6 |

**unsafe 사용 0건.** 이 상품은 판매종료인데도 `availability=IN_STOCK`,
`product_status=SALE`을 VERIFIED로 들고 있으나 답변 근거로 나가지 않습니다.

repository 단위뿐 아니라 `ProductKnowledgeService` 기본 경로 조회에서도 동일하게 차단됩니다.

---

## Component Subject 검증

| 상품 | 질문 | 기대 | 결과 |
|---|---|---|---|
| `11848813000` (TV+STB 패키지) | 셋톱박스도 삼성 제품인가요? | 차단 | **차단** |
| `11848813000` | 스탠드 제조사가 삼성인가요? | 차단 | **차단** |
| `9866761076` (무빙스탠드 패키지) | 같이 오는 스탠드도 삼성 제품인가요? | 차단 | **차단** |
| `11784488741` | 셋톱박스 제조사도 삼성인가요? | 차단 | **차단** |
| `11792827130` | 구성품 원산지가 어디인가요? | 차단 | **차단** |
| `11779070305` (STB 단독) | 이 셋톱박스 제조사는 어디예요? | 허용 | `manufacturer=이노피아테크` |
| `11779070305` | 이 셋톱박스 브랜드가 어디예요? | 허용 | `brand=SHAKS` |
| `9866761076` | 이 상품 브랜드가 뭐예요? | 허용 | `brand=삼성` |

listing-level 식별정보가 구성품으로 상속된 사례 **0건**.

---

## Brand / Manufacturer 검증

| 상품 | 질문 | 기대 필드 | 답변 |
|---|---|---|---|
| `12601323000` | 브랜드가 뭐예요? | `brand` | 삼성 |
| `12601323000` | 삼성에서 만든 거 맞죠? | `manufacturer` | 삼성전자 |
| **`12101311850`** | **삼성에서 만든 거 맞죠?** | `manufacturer` | **(주)오제플러스** |
| `12101311850` | 브랜드가 뭐예요? | `brand` | 오베닉 |
| `11779070305` | 삼성에서 만든 거 맞죠? | `manufacturer` | 이노피아테크 |

기대하지 않은 식별 필드가 섞여 나온 사례 **0건**.
`12101311850`은 brand가 `오베닉`이라 brand로 답했다면 제조사 오답이 됐을 사례입니다.

---

## Product Line 검증

| 상품 | 질문 | 결과 |
|---|---|---|
| `12601323000` | 오디세이인가요? | `model_name`에 "오디세이" 존재 → 근거 사용 |
| `11844406044` | 오디세이인가요? | 값에 없음 → **UNKNOWN**, evidence·prompt 빈 문자열 |
| `13239109816` | 무빙스타일인가요? | `brand=무빙스타일` → 근거 사용 |
| `10245133834` | 스마트모니터인가요? | 값에 없음 → UNKNOWN |
| `12601323000` | 무빙스타일인가요? | 값에 없음 → UNKNOWN |

근거 없는 product-line을 추론한 사례 0건, negative inference **0건**
(부정 단어가 evidence/prompt에 들어간 경우 없음).

---

## Learning Conflict 검증

`services/learning_evidence_policy.py`는 변경하지 않았습니다.
`PRODUCT_FACT_VS_LEARNING_CONFLICT` 정책과 authority 순서가 그대로이며,
`test_learning_authority_and_model_identity.py`,
`test_learning_hardening_golden.py`,
`test_learning_product_topic_compatibility.py`가 §43의 793 passed에 포함되어 통과했습니다.

---

## Cross-product Leakage

두 축으로 검증했습니다.

1. **쌍 비교** — 서로 다른 상품 **12쌍** × 3개 질문. 모든 `safe_fact.product_id`가
   질문한 `product_id`와 일치하는지 확인 → **위반 0건**
2. **전수 replay** — 실제 문의 2,772건 전부에서 동일 검사 → **위반 0건**

미수집 상품(`product_id`는 있으나 Product Facts에 없음) 66건에서
다른 상품 fact가 반환된 사례도 **0건**입니다.

model_name 기반 fallback 등 모호한 cross-product 경로는 도입하지 않았습니다.

---

## Product ID Coverage

운영 DB를 read-only로 재측정했습니다.

| 항목 | Phase 11-A | **현재** |
|---|---|---|
| 문의 총수 | 2,772 | 2,772 |
| `product_id` 보유 | 717 | 717 |
| unique product_id | 79 | 79 |
| exact match | 59 | 59 |
| unmatched | 20 | 20 |

문의 단위로는 exact join 성공 651건, `product_id`는 있으나 미수집 66건,
`product_id` 없음 2,055건입니다.

---

## Q&A Scenario Matrix

시나리오는 DB에서 **발견**했습니다 — 존재하지 않는 사실을 기대값으로 만들지 않았습니다.

| 구분 | 개수 |
|---|---|
| 주제(topic) 검증 행 | 47 |
| Positive | 34 |
| UNKNOWN | 20 |
| NEEDS_REVIEW 표본 | 15 |
| CONFLICT 표본 | 15 |
| Safety | 39 |
| 복합질문 | 15 |
| cross-product 쌍 | 12 |
| 숫자 fact 표본 | 21 |
| Injection 프로브 | 4 |
| **총 시나리오** | **246** |

24개 주제(화면·해상도·HDMI·DP·USB·블루투스·와이파이·VESA·크기·무게·구성품·리모컨·
스탠드·셋톱박스·OTT·피벗·설치·브랜드·제조사·제품라인·주사율·패널·스피커·전력)를 모두 포함했습니다.

---

## Positive Scenario

34건. 각 시나리오에서 제공된 fact가 전부
`verification_status=VERIFIED`, `resolution_status ∉ {CONFLICT, NEEDS_REVIEW}`,
provenance ≥ 1을 만족했습니다. 위반 **0건**.

34건 모두 `prompt_block()`에 `PRODUCT_FACTS` 블록이 생성되어
provider 프롬프트까지 도달했습니다.

---

## UNKNOWN Scenario

20건. 해당 상품에 그 fact가 실제로 없는 조합만 골랐습니다.

- Product Facts evidence 없음 → `evidence_text()`·`prompt_block()` 빈 문자열
- 부정 단어 유입 **0건**
- "정보 없음"이 "기능 없음"으로 바뀐 사례 **0건**

---

## NEEDS_REVIEW / CONFLICT

각 15건, 상품당 최대 3건으로 분산 샘플링했습니다.

| 검사 | 결과 |
|---|---|
| 해당 field가 `safe_facts`에 포함 | **0건** |
| 해당 field가 `prompt_block()`에 등장 | **0건** |

---

## Negative / Safety Scenario

39건. 구성 —
`COLLECTION_FAILED` 1,
component subject 8,
brand/manufacturer 5,
product line 5,
missing item 10,
DPS 질문 10.

wrong fact 사용 **0**, unsupported claim **0**.

---

## 복합질문

15건. sub-question이 서로 독립적으로 처리됨을 확인했습니다.

| 질문 | 요청 필드 | 제공 |
|---|---|---|
| 블루투스 되고 넷플릭스도 되나요? | bluetooth_* | bluetooth_* (넷플릭스는 미매핑 → 미답변) |
| 벽걸이 베사 규격이랑 무게 알려주세요. | vesa_mm + weight_* | 둘 다 제공 |
| **삼성 제품인가요? 셋톱박스도 같이 오나요?** | manufacturer | **제공 없음** — 셋톱박스 언급으로 component gate 발화 |
| 배송은 언제 오고 리모컨도 포함인가요? | (없음) | 없음 — 배송은 DPS, 리모컨은 미매핑 |
| HDMI 몇 개이고 USB도 있나요? | hdmi_* + usb_* | 둘 다 제공 |
| 제조사가 어디이고 원산지는 어디인가요? | manufacturer + country_of_origin | manufacturer만(원산지는 held) |

하나의 fact가 다른 sub-question을 억지로 채운 사례 **0건**입니다.

---

## Prompt Injection / Evidence

Product Facts는 **data**로만 프롬프트에 들어갑니다.

| 검사 | 결과 |
|---|---|
| 주입 문자열 3종이 prompt에 등장 | **0건** |
| prompt에 `RULES:` 블록 존재 | 예 |
| prompt에 "Never say a feature is absent" 규칙 존재 | 예 |
| DB provenance 중 `무시`/`ignore`/`instruction` 포함 source_text | **0건** |

`prompt_block()`은 fact를 `- field: … value: … verification: …` 형태의
구조화된 evidence 목록으로만 렌더링하며, 원문을 지시문 위치에 넣지 않습니다.

---

## Provenance

`safe_facts`가 되려면 `NO_ACTIVE_PROVENANCE`·`PROVENANCE_NOT_VERIFIED`를 통과해야 합니다.
숫자 fact 21건 표본에서 provenance 누락 **0건**이며,
각 fact가 `source_type`/`source_locator`/`source_text`로 원문까지 추적됩니다.

예: `hdmi_port_count=2` ← `IMAGE_OCR "HDMI 2개"` (provenance 2건)

---

## 숫자 Fact

| 상품 | field | 값 | 단위 | 근거 원문 |
|---|---|---|---|---|
| `10194603339` | `hdmi_port_count` | 1 | 개 | `HDMI 1개` |
| `10198648691` | `hdmi_port_count` | 2 | 개 | `HDMI 2개` |
| `10198648691` | `usb_port_count` | 2 | 개 | `USB Ports 2` |
| `10194603339` | `refresh_rate` | 180 | Hz | `180Hz 주사율` |
| `10198648691` | `refresh_rate` | 60 | Hz | `Max 60 Hz` |
| `10198648691` | `weight_with_stand_kg` | 6.6 | kg | `제품 무게 (스탠드 포함) 6.6 kg` |

**축 보존 확인**

- `vesa_mm = {"horizontal":100, "vertical":100}` → 근거 `베사 100 x 100`,
  근거문 렌더링 `vesa_mm: 100x100mm` — 한 축만 쓰거나 숫자를 합치지 않습니다
- `dimensions_with_stand_mm = {"width":716.1, "height":517, "depth":193.5}` — 3축 유지

단위 혼동(mm/cm/kg/Hz/개) 사례 **0건**.

---

## 포함 / 별도구매 / 호환

의미가 다른 개념이 **서로 다른 field 이름으로 분리 저장**되어 있어
retrieval 단계에서 혼동될 구조가 아닙니다.

| 의미 | field 예 |
|---|---|
| 포함 / 기본 제공 | `remote_control_included`, `power_cable_included`, `hdmi_cable_included`, `accessory_shelf_included` |
| 존재 / 탑재 | `hdmi_present`, `usb_present`, `bluetooth_present` |
| 지원 / 가능 | `ott_supported`, `password_lock_supported`, `package_auto_pivot_supported` |
| 호환 규격 | `vesa_mm`, `accessory_vesa_mm` |

"셋톱박스 사용 가능"과 "셋톱박스 포함"이 같은 field를 공유하지 않으며,
"벽걸이 설치 가능"(`vesa_mm`)과 "벽걸이 브라켓 포함"(별도 `_included` 계열)도 분리됩니다.

---

## Missing Item

파이프라인 분석 단계에서 확인했습니다.

| 문의 | `can_generate_answer` |
|---|---|
| 리모컨이 안 왔어요 | **False** |
| 스탠드가 빠졌어요 | **False** |
| 구성품이 누락됐어요 | **False** |
| 벽걸이 부품이 없어요 | **False** |

네 경우 모두 답변 생성 자체가 차단되므로 Product Facts가 관여할 수 없습니다.

**검증 과정에서 정정한 사항**: 조회 계층(`facts_for_inquiry`)만 직접 호출했을 때
"스탠드가 빠졌어요"에 `stand_type`, "벽걸이 부품이 없어요"에 `vesa_mm`이 반환되어
False Positive 3건으로 잡혔습니다. 그러나 실제 파이프라인에서는 상위 단계가
`can_generate_answer=False`로 먼저 차단하므로 이 결과가 답변이 되는 경로가 없습니다.
**계측 오류였고 실제 안전 결함이 아닙니다.**

replay 2,772건에서 누락문의 22건이 식별되었고,
그중 Product Facts가 답변 근거로 사용된 사례는 **0건**입니다.

---

## DPS Routing

| 문의 | `requires_dps_lookup` | `requires_order_id` |
|---|---|---|
| 언제 배송돼요? | True | True |
| 설치 언제 오나요? | True | True |
| 기사님 언제 오나요? | True | True |
| 제 주문 배송 상태가 어떻게 되나요? | True | True |
| 오늘 설치 예정 맞나요? | True | True |
| HDMI 몇 개예요? | False | False |

이들 DPS 질문에 Product Facts가 제공한 fact는 **0건**입니다.

replay에서 `requires_dps_lookup=True`인 문의 중 Product Facts가 fact를 제공한 것은 **2건**인데,
둘 다 **복합문의**였습니다.

| id | 문의 성격(원문 미인용) | 제공 fact |
|---|---|---|
| 2268 | 수령 시기를 묻고, 동시에 결제한 벽걸이 옵션과 스탠드 구성이 맞는지 확인 | `stand_type` |
| 2681 | 배송 날짜·설치비용을 묻고, 동시에 화면 크기와 벽걸이 설치 예정을 언급 | `screen_size` |

원문은 고객이 작성한 문의 텍스트이므로 인용하지 않고 성격만 적었습니다.

배송 일정 부분은 여전히 DPS가 담당하고(`requires_dps_lookup=True` 유지),
Product Facts는 제품 사양 sub-question에만 답합니다. **DPS bypass 0건**입니다.

일반 배송/설치 정책 fact(`delivery_fee`, `arrival_guarantee` 등)는
`FIELD_TOPICS`에 매핑돼 있지 않아 현재 주문 일정 질문에 도달하지 않습니다.

---

## Auto-post Gate

`services/auto_processing_eligibility_service.py`는 변경하지 않았습니다.
차단되는 fact는 `excluded_facts`로 분류되어 `safe_facts`에 들어가지 않으므로

- `has_safe_facts` → False
- `supports_question()` → False
- `prompt_block()` / `evidence_text()` → 빈 문자열

이 되고, `product_fact_guard.sensitive`가 참인데 `current_fact_verified`가 서지 않아
기존 `PRODUCT_FACT_NOT_VERIFIED` 차단이 걸립니다.

`test_auto_post_gate_server_cases.py`, `test_auto_post_policy_v7.py`,
`test_pre_generation_gate.py`가 §43의 793 passed에 포함되어 통과했습니다.

---

## 정상 Auto-post 후보

안전성을 이유로 Product Facts가 사실상 사용 불가능해지지 않았음을 확인했습니다.

| 기준 | 건수 |
|---|---|
| matrix positive 중 prompt까지 도달 + 위반 없음 | **34** |
| replay에서 exact join + usable fact + `can_generate_answer` + DPS 불필요 + 누락문의 아님 | **56** |

---

## False Positive / False Negative

| 구분 | 건수 | 내용 |
|---|---|---|
| **False Positive** | **0** | 조회 계층 계측에서 3건이 잡혔으나 전부 상위 `can_generate_answer=False`로 차단되는 누락문의였음(계측 오류) |
| **False Negative (safety gate 과차단)** | **0** | — |
| 참고: 매핑 공백 | 12 | 안전 게이트가 아니라 `FIELD_TOPICS`에 해당 field가 없어 질문이 도달하지 못함 |

매핑 공백 12건의 field: `power_cable_included`, `hdmi_cable_included`,
`remote_control_included`, `set_top_box_wifi_present`, `ott_supported`,
`installation_method`, `power_consumption_typical_w`, `power_consumption_max_w`.

당초 13건으로 잡혔던 것 중 1건(`10198648691` / "오디세이인가요?")은
`PRODUCT_LINE_NOT_IN_VALUE`로 차단된 것이며, 이 상품은 실제로 오디세이가 아니므로
**올바른 차단**입니다. 탐지기가 순진했던 것으로 False Negative에서 제외했습니다.

### 구어체 표현 커버리지

실제 고객 표현 29개로 측정 — **21개 매핑 성공 / 8개 실패**.

실패: `구성품이 뭐가 들어있나요?`, `리모컨 포함인가요?`, `셋톱도 같이 주나요?`,
`넷플 볼 수 있나요?`, `설치는 어떻게 하나요?`, `전기 얼마나 먹나요?`,
`휴대폰이랑 연결돼요?`, `유튜브 되나요?`

앞 5개는 미매핑 field, 뒤 3개는 매핑된 field의 동의어 표현입니다
(`전기 얼마나 먹나요?` → `소비전력`, `휴대폰이랑 연결돼요?` → 블루투스,
`유튜브 되나요?` → OTT). 모두 결과는 SAFE_UNKNOWN이며 오답이 아닙니다.

---

## 실제 문의 Replay

운영 DB를 read-only로 열어 **전체 2,772건**을 offline replay했습니다.
실제 등록·전송·DPS 실행은 하지 않았습니다.

| 항목 | 건수 |
|---|---|
| 총 문의 | 2,772 |
| `product_id` 없음 | 2,055 |
| exact join 성공 | 651 |
| `product_id` 있으나 미수집 | 66 |
| usable fact 있음 | 58 |
| usable fact 없음 | 593 |
| DPS 필요 | 982 |
| 누락문의 | 22 |
| 답변생성 차단 | 30 |
| 안전 자동처리 후보 | 56 |

| 안전 지표 | 결과 |
|---|---|
| cross-product leakage | **0** |
| unsupported claim | **0** |
| 누락문의에 Product Facts 근거 제공 | **0** |
| DPS bypass | **0** (fact 제공 2건은 복합문의, 라우팅 불변) |

---

## Inquiry Coverage

의미 있는 분모로 다시 계산했습니다.

| 항목 | 건수 |
|---|---|
| 전체 문의 | 2,772 |
| **상품사실 주제를 실제로 묻는 문의** | **557** |
| 그중 exact join 성공 | 176 |
| 그중 usable fact 제공 | **58 (join 대비 33.0%)** |

557 → 176의 감소는 `product_id` 부재(특히 `CUSTOMER_INQUIRY` 1,110건 전부)가 원인이고,
176 → 58은 해당 상품에 그 주제의 VERIFIED fact가 없거나 매핑 공백 때문입니다.

숫자를 좋게 만들기 위해 safety gate를 완화하지 않았습니다.

---

## 성능

| 구간 | n | median | p95 | max |
|---|---|---|---|---|
| repository (listing + facts) | 192 | 3.75 ms | 4.92 ms | 7.39 ms |
| **service retrieval (answer path)** | 192 | **3.26 ms** | **3.92 ms** | **5.13 ms** |

Phase 11 전반의 약 3 ms 수준과 동일합니다.
DB가 142 MB로 커졌지만 조회가 exact key 기반이고 인덱스가 동일해 회귀가 없습니다.
외부 GPT/DPS/network는 포함하지 않았습니다.

---

## DB 무변경

| 항목 | 값 |
|---|---|
| SHA-256 (작업 전) | `e0cdd363…6f55a078` |
| SHA-256 (모든 테스트 후) | `e0cdd363…6f55a078` **동일** |
| size / mtime | 142,131,200 / 2026-08-29 23:42:01 **동일** |
| WAL / SHM / journal | **없음** |

Product Facts 테스트가 DB에 write한 흔적이 없습니다.

---

## 전체 테스트

**§43 Product Facts 계층 관련 20개 파일**

```
793 passed in 221.83s (0:03:41)
```

repository / `ProductKnowledgeService` / AnswerService·Hybrid 경로 /
AnswerValidator / `AutoProcessingEligibilityService` / Learning conflict /
collection_status / component subject / brand·manufacturer / missing item /
DPS routing / stale DPS / real_db / E2E — **0 failed, 0 skipped**.

**§44 전체**

```
3526 passed in 1091.84s (0:18:11)
```

| 시점 | passed | failed | skipped |
|---|---|---|---|
| Phase 11-E baseline | 3,526 | 0 | 0 |
| **Phase 11-F** | **3,526** | **0** | **0** |

테스트를 삭제·skip·xfail 처리해 통과시킨 항목은 없습니다.
이번 Phase에서 테스트를 추가하지 않았으므로 개수가 동일합니다.

---

## 수정사항

**production 코드 수정 0건. 테스트 수정 0건.**

§45에 따라, 모든 검증이 통과하고 수정이 필요하지 않았으므로
불필요한 코드를 건드리지 않았습니다.
Phase 11-B~E에서 구현한 gate가 실제 Q&A 통합 계약까지 그대로 충족합니다.

이번 Phase의 산출물은 이 보고서 1개이며,
검증 하네스(`integration_matrix.py`, `run_matrix.py`, `replay.py`, `routing_probe.py`)는
저장소 밖 scratchpad에만 두었습니다.

---

## 잔여 위험

**MEDIUM — topic 매핑 공백 12건**
`remote_control_included`, `power_cable_included`, `ott_supported`,
`set_top_box_*`, `installation_method`, `power_consumption_*`에 VERIFIED fact가 있으나
질문이 도달하지 못합니다. 오답이 아니라 미답변이며, 확장은 별도 판단 사항입니다.
특히 `set_top_box_*`는 구성품 고유 사실인데 `component_scope`가 `accessory_` 접두사만
인식하므로 매핑 전에 설계가 선행되어야 합니다.

**MEDIUM — 구어체 동의어 미인식 3종**
`전기 얼마나 먹나요?`, `휴대폰이랑 연결돼요?`, `유튜브 되나요?`가
매핑된 field에 도달하지 못합니다. 키워드 추가로 해결 가능하나 이번 범위 밖입니다.

**MEDIUM — `product_id` 부재로 인한 커버리지 한계**
`CUSTOMER_INQUIRY` 1,110건은 Naver 응답에 상품 식별자가 없어 조회 자체가 불가능합니다.
`PRODUCT_INQUIRY` 중 945건은 `raw_json`에 값이 있으나 컬럼이 비어 복구 가능합니다.

**LOW — usable fact 비율 33%**
exact join된 상품사실 문의 176건 중 58건만 근거를 제공합니다.
NEEDS_REVIEW·CONFLICT가 늘어난 최신 DB 특성이 반영된 결과이며,
품질 개선은 상품DB 쪽 과제입니다.

---

## 핵심 지표

| # | 항목 | 값 |
|---|---|---|
| 1 | Q&A scenario 총 개수 | **246** |
| 2 | Positive scenario | 34 |
| 3 | UNKNOWN scenario | 20 |
| 4 | NEEDS_REVIEW sample | 15 |
| 5 | CONFLICT sample | 15 |
| 6 | Safety scenario | 39 |
| 7 | 복합질문 | 15 |
| 8 | 실제 문의 replay | **2,772 (전수)** |
| 9 | cross-product leakage | **0** |
| 10 | unsupported claim | **0** |
| 11 | DPS bypass | **0** |
| 12 | Missing Item 잘못된 auto-answer | **0** |
| 13 | False Positive | **0** |
| 14 | False Negative (gate 과차단) | **0** (매핑 공백 12건은 별도) |
| 15 | safe positive auto-processing 가능 | 34 (matrix) / **56** (replay) |
| 16 | Product Facts retrieval median / p95 / max | **3.26 / 3.92 / 5.13 ms** |
| 17 | 전체 tests passed / failed / skipped | **3,526 / 0 / 0** |

---

## 최종 판정

# PHASE 11-F CONDITIONAL READY — 안전성은 충족하나 Product Facts 활용률 개선 필요

§50의 READY 조건을 하나씩 대조합니다.

| 조건 | 결과 |
|---|---|
| 최종 DB identity 일치 | ○ `e0cdd363…6f55a078` |
| runtime이 최종 DB 사용 | ○ override 없이 기본 경로에서 확인 |
| cross-product leakage = 0 | ○ |
| unsupported claim = 0 | ○ |
| DPS bypass = 0 | ○ (400건 대조 실험에서 라우팅 차이 0) |
| Missing Item 잘못된 auto-answer = 0 | ○ |
| unsafe COLLECTION_FAILED fact 사용 = 0 | ○ |
| NEEDS_REVIEW/CONFLICT unsafe 사용 = 0 | ○ |
| component inheritance 오류 = 0 | ○ |
| brand/manufacturer semantic 오류 = 0 | ○ |
| 전체 테스트 0 failed / 0 skipped | ○ 3,526 passed |
| DB hash 작업 전후 동일 | ○ |

**안전성 조건은 전부 충족했습니다.**

그럼에도 `READY`가 아니라 `CONDITIONAL READY`로 판정하는 이유는 §51에 따라
False Negative 성격의 활용률 문제를 숨기지 않기 위해서입니다.

- `FIELD_TOPICS` 매핑 공백 **12건** — 리모컨·구성품·셋톱박스·OTT·설치·소비전력에
  VERIFIED fact가 있으나 질문이 도달하지 못함
- 구어체 동의어 **3종** 미인식 — `전기 얼마나 먹나요?`, `휴대폰이랑 연결돼요?`, `유튜브 되나요?`
- 상품사실 문의 557건 중 실제 근거 제공은 **58건**

이들은 전부 **오답이 아니라 미답변(SAFE_UNKNOWN)** 이며 안전 게이트의 과차단도 아닙니다.
Product Facts를 실제 운영에 사용하는 데 장애가 되는 안전 문제는 없습니다.
