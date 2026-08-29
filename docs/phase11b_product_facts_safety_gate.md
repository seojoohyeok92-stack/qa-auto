# Phase 11-B — Product Facts Retrieval Safety Gate 보강

작업일: 2026-08-29 · 최신 Product Facts DB 반입 **없음** · Git commit/push **없음**

---

## 1. 시작 상태

| 항목 | 값 |
|---|---|
| branch | `main` |
| HEAD | `3950e8b` (`26.8.28v2 - 문의 단건 진단 Export 추가`) |
| 작업 시작 시 `git status` | `?? docs/phase11a_product_facts_integration_audit.md` 1건만 존재 |
| 개발 PC `data/product_facts.db` | 60,170,240 bytes, mtime 2026-08-25 12:30:32, SHA-256 `cddf3082…ac82ac4c` |
| 상품DB 현행 DB | 60,170,240 bytes, mtime 2026-08-29 03:32:48, SHA-256 `8fe643c5…4e563f93` |

Phase 11-A 보고서는 untracked 상태 그대로 두었고 삭제하거나 commit하지 않았습니다.
작업 시작 시점에 그 외의 사용자 변경은 없었으므로, 아래 "수정 파일"은 전부 이번 Phase가 만든 것입니다.

### 사양서 전제와 다른 사실 2가지 (먼저 보고합니다)

**(1) 이 PC의 어떤 DB에도 판매종료 listing이 없습니다.**

사양서 §6은 최신 DB에 `93 COLLECTION_SUCCESS + 1 COLLECTION_FAILED`가 있다고 했고,
§24는 `13074225226`이 최신 DB에서 COLLECTION_SUCCESS가 아님을 확인하라고 했습니다.
실제로 두 DB를 모두 읽어 확인한 결과는 다릅니다.

| DB | listings | collection_status 분포 | `13074225226` |
|---|---|---|---|
| 개발 PC (구버전) | 94 | `COLLECTION_SUCCESS` 94 | `COLLECTION_SUCCESS` |
| 상품DB 현행 | 94 | `COLLECTION_SUCCESS` 94 | `COLLECTION_SUCCESS` |

`13074225226`은 두 DB 모두에서 정상 수집 상태이며, 이름은
"삼성전자 LS27FM501E-2MO 삼성 무빙스타일 M5 스마트 모니터 … 웜 화이트"입니다.
사양서가 말한 최종 DB(판매종료 1건 포함)는 이 PC의 어느 경로에도 없습니다.
따라서 §24의 실물 회귀 검증은 **수행할 수 없었고**, 대신 동일 상품에 대해
임시 사본으로 상태를 바꿔 gate 동작을 검증했습니다(§10).

**(2) `COLLECTION_FAILED`가 실제 enum이고 `PRODUCT_DELISTED`는 상태값이 아닙니다.**

이름을 추측하지 말라는 §7 지시에 따라 Product Facts 수집기 코드를 직접 확인했습니다.
`collector.py:217`과 `browser_probe.py:75-78`이 `collection_status`를 기록하는 유일한 지점이며,
값은 `COLLECTION_SUCCESS` / `COLLECTION_FAILED` 두 가지뿐입니다.
`PRODUCT_DELISTED`는 수집 실패의 **사유**이지 저장되는 상태가 아니므로 새 enum을 만들지 않았습니다.

---

## 2. Root Cause

### R1이 겨냥한 위험의 실제 크기 — 측정 결과 재정의

Phase 11-A는 "`collection_status`를 읽어 판단하는 코드가 없다"를 BLOCKER로 기록했습니다.
그 사실 자체는 맞습니다. 다만 이번에 실제 데이터를 측정한 결과, **위험의 소재가 예상과 달랐습니다.**

측정 1 — volatility별 필드 구성 (ACTIVE 기준):

| volatility | 필드 종류 | 대표 필드 |
|---|---|---|
| `DYNAMIC_LISTING_FACT` | 8 | `availability`, `product_status`, `listing_price`, `review_count` … |
| `SEMI_STATIC_POLICY_FACT` | 40 | `delivery_fee`, `change_of_mind_return_period_days`, `as_service_center_phone` … |
| `STATIC_PRODUCT_FACT` | 나머지 | `hdmi_port_count`, `screen_size`, `vesa_mm` … |

사양서 §8이 "과거 VERIFIED라도 차단해야 한다"고 지목한 `availability`와 `product_status`는
**이미 `DYNAMIC_LISTING_FACT`** 이고, 기존 `UNUSABLE_VOLATILITY` 규칙이 collection_status와 무관하게
**항상** 차단하고 있었습니다. 즉 그 부분은 이미 안전했습니다.

측정 2 — 질문이 실제로 요청할 수 있는 필드:

`FIELD_TOPICS`가 매핑하는 66개 필드 중 DB에 존재하는 **63개가 전부 `STATIC_PRODUCT_FACT`** 입니다.
`DYNAMIC` 0개, `SEMI_STATIC` 0개.

**결론**: 오늘 고객 질문이 도달할 수 있는 Product Facts는 전부 정적 하드웨어 사양입니다.
따라서 R1 gate는 **지금 새는 구멍을 막는 수리가 아니라, 방어층(defence in depth)**입니다.
`FIELD_TOPICS`는 손으로 관리하는 목록이고 누군가 배송·정책 topic을 추가하는 순간
volatility gate와 collection gate가 하중을 받게 되므로, 그때를 대비해 지금 넣는 것이 맞습니다.
이 사실을 테스트로 못 박아 두었습니다(`test_real_db_topic_map_only_requests_static_fields`).

이 재정의를 반영해 gate 정책을 §8의 최소 계약보다 한 칸 넓혔습니다 — 아래 §3 참조.

### R2가 겨냥한 위험 — 실제 데이터로 확인된 오답

이쪽은 방어층이 아니라 **실재하는 오답 가능성**입니다.

| product_id | 상품명 | brand | manufacturer |
|---|---|---|---|
| `11848813000` | 삼성 85인치 4K UHD 스마트 비즈니스TV**+OTT 구글TV 셋탑박스** | 삼성 | 삼성전자 |
| `11779070305` | **샥스 G1 셋탑박스** 넷플릭스 유튜브 구글TV 4K TV OTT | SHAKS | **이노피아테크** |

패키지 listing의 manufacturer는 `삼성전자`지만, 그 패키지에 들어가는 셋톱박스를 단독으로 파는
listing의 manufacturer는 `이노피아테크`입니다.
"셋톱박스도 삼성 제품인가요?"에 패키지의 manufacturer를 쓰면 **사실과 다른 제조사를 답하게 됩니다.**

`product_classifications.product_type`으로 이 문제를 풀 수 없다는 것도 확인했습니다.
`SETTOP_ACCESSORY`로 분류된 4건 중 **3건이 실제로는 TV+셋톱박스 패키지**입니다.
"디스플레이 사실 보유 여부"도 신호가 되지 못했습니다(BUSINESS_TV_SIGNAGE 16건 중 13건이 미보유).
그래서 listing 분류가 아니라 **질문의 주어**로 판정하도록 설계했습니다.

### R4가 겨냥한 위험 — brand가 제조사를 증명하지 못함

`brand`의 ACTIVE VERIFIED 값 분포: `삼성` 63, **`오디세이` 8, `스마트모니터` 6, `무빙스타일` 5**, `오베닉` 1, `SHAKS` 1.
`manufacturer`: `삼성전자` 84, `(주)오제플러스` 1, `이노피아테크` 1.

`brand`는 제조사와 제품라인을 같은 칸에 담고 있습니다.
변경 전 코드는 `("제조사", "브랜드", "made in", "원산지", "제조국")` 하나의 topic이
`manufacturer` + `brand` + `country_of_origin` 세 필드를 **한꺼번에** 요청했습니다.
그 결과 "삼성 제품인가요?"라는 질문에 `brand=오디세이`가 근거로 따라 들어갈 수 있었습니다.

---

## 3. R1 — collection_status Gate

### 정책

중앙 안전 경계인 `ProductKnowledgeService._exclusion_reason()`에 조건을 하나 추가했습니다.
Answer path 여러 곳에 조건문을 복제하지 않았습니다(§10 준수).

| collection_status | `STATIC_PRODUCT_FACT` | `SEMI_STATIC_POLICY_FACT` | `DYNAMIC_LISTING_FACT` |
|---|---|---|---|
| `COLLECTION_SUCCESS` | 기존 정책 그대로 사용 | 기존 정책 그대로 사용 | 기존대로 항상 차단 |
| 그 외 (`COLLECTION_FAILED`, NULL, 미지값) | **그대로 사용** | **차단** | 기존대로 항상 차단 |

새 사유 코드: **`COLLECTION_STATUS_NOT_CURRENT`**
(§10이 제시한 후보 중 기존 명명 관습 — `VOLATILE_LISTING_FACT`, `MODEL_SCOPE_MISMATCH`처럼
실패한 조건을 명사구로 적는 방식 — 에 가장 가까운 이름을 골랐습니다.)

**`SEMI_STATIC_POLICY_FACT`를 §8의 최소 계약보다 넓게 차단한 이유**:
이 40개 필드는 배송비, 단순변심 반품 기간, A/S센터 전화번호, 공식파트너 상태처럼
**제품이 아니라 그 listing의 제안 조건**을 기술합니다.
오늘 읽히지 않는 listing의 배송 마감시각을 고객에게 말하는 것은
판매자가 지킬 수 없을지 모르는 약속입니다. §9의 fail-closed 원칙에 따라 차단합니다.
반대로 화면 크기·포트 수·치수 같은 정적 사양은 판매가 끝나도 변하지 않으므로
**삭제하지도 UNKNOWN으로 만들지도 않았습니다**(§8 명시 요구).

### Fail-safe

`collection_status`가 `None`, 빈 문자열, `"UNKNOWN"`, `"COLLECTION_PENDING"`,
소문자 `"success"`, 공백 문자열인 경우 모두 "현재 상태 아님"으로 처리합니다.
알아볼 수 없는 상태는 listing이 살아 있다는 증거가 아니기 때문입니다.
비교는 `strip().upper()` 후 정확히 `COLLECTION_SUCCESS`와 일치할 때만 통과합니다.

---

## 4. R2 — Package / Component Subject

### 판정 방식

새 함수 `asks_about_a_bundled_component(question)` 하나로 판정합니다.

1. 질문에 구성품 단어가 있는가 — `셋톱박스`, `셋탑박스`, `set-top`, `stb`, `스탠드`, `거치대`,
   `받침대`, `모니터암`, `브라켓`, `브래킷`, `액세서리`, `악세서리`, `부속품`, `구성품`
2. 그 단어 앞에 자기지시 표지(`이 `, `본 `, `해당 `, `이번 `)가 붙어 있는가

구성품 단어가 있고 자기지시 표지가 **없으면** → 그 질문의 주어는 동봉된 구성품으로 판정합니다.
이때 **본체 scope의 `brand` / `manufacturer` / `country_of_origin`만** 근거에서 제외합니다.
새 사유 코드: **`COMPONENT_SUBJECT_UNRESOLVED`**

`stand_type`, `accessory_*` 같은 구성품 자체의 사양은 영향받지 않습니다.
listing 분류 taxonomy를 새로 만들지 않았습니다(§13 준수).

### False Positive 방지 (§15)

자기지시 표지가 §15의 구분을 그대로 구현합니다.

| 질문 | 판정 | 결과 |
|---|---|---|
| `이 스탠드 브랜드가 어디인가요?` | listing 자신 | brand 사용 가능 |
| `이 셋톱박스 제조사는 어디인가요?` | listing 자신 | manufacturer 사용 가능 |
| `본 상품 브랜드가 뭔가요?` | listing 자신 | 사용 가능 |
| `셋톱박스도 삼성 제품인가요?` | 구성품 | 차단 |
| `스탠드 제조사가 삼성인가요?` | 구성품 | 차단 |
| `브랜드가 뭔가요?` (구성품 단어 없음) | listing 자신 | 영향 없음 |

**보수적 판정 1건을 명시합니다**: 스탠드 단독 listing에 `이 `를 붙이지 않고
"스탠드 제조사가 어디예요?"라고 물으면 차단되어 SAFE_UNKNOWN이 됩니다.
listing 분류로는 이를 구분할 수 없다는 것이 위 §2에서 측정된 사실이고,
§14가 "근거 없는 답변을 막는 것"을 목표로 명시했으므로 fail-closed를 택했습니다.
오답이 아니라 커버리지 손실입니다.

### 실제 문의 코퍼스 오탐 실측

개발 PC의 실제 문의 2,772건에 판정기를 적용했습니다.

| 항목 | 건수 |
|---|---|
| 식별 필드(brand/manufacturer/origin)를 요청하는 문의 | 41 |
| 구성품 주어로 판정된 문의 | 249 |
| **둘 다 해당 = 실제로 차단되는 문의** | **17 (전체의 0.6%)** |

차단되는 17건에는 의도한 대상이 그대로 들어 있습니다.

- `스탠드도 삼성 정품인가요`
- `스탠드는 자사브랜드 상품이라 하셨는데, 오베닉스탠드로 오는건가요?`
- `현재 판매가는 모니터, 거치대 패키지 가격인데, 이동식 거치대는 삼성 제품이 아니라서…`

세 번째는 고객 스스로 "이동식 거치대는 삼성 제품이 아니다"라고 적은 문의입니다.
패키지의 제조사를 구성품에 상속하면 안 된다는 것을 실제 문의가 증언합니다.

---

## 5. R4 — brand / manufacturer 계약

### topic 분리

하나였던 항목을 다섯으로 나눴습니다.

| 질문 키워드 | 요청 필드 |
|---|---|
| `브랜드` | `brand` |
| `제조사`, `제조원`, `만든 곳`, `made in`, `제조업체` | `manufacturer` |
| `원산지`, `제조국`, `생산지`, `어디서 만든`, `어디서 생산` | `country_of_origin` |
| `삼성 제품`, `삼성전자 제품`, `삼성에서 만든`, `삼성 정품` … | `manufacturer` **만** |
| `오디세이`, `스마트모니터`, `무빙스타일` | `brand`, `model_name` |

"삼성 제품인가요?"에 `brand`를 주지 않는 것이 §18의 핵심입니다.
`brand=오디세이`인 상품에서 확인했습니다 — 요청 필드는 `('manufacturer',)` 하나이고
답변 근거는 `manufacturer=삼성전자`입니다.

### Product-line 질문 (§19)

제품라인 질문은 `brand` 또는 `model_name`의 **저장된 값 안에 그 라인 이름이 실제로 적혀 있을 때만**
근거가 됩니다. 새 사유 코드: **`PRODUCT_LINE_NOT_IN_VALUE`**

실제 DB 확인:

| 상품 | "오디세이 제품인가요?" 결과 |
|---|---|
| `12601323000` | `model_name = "삼성전자 오디세이 G5 G50F LS32FG500"` → 근거로 사용 |
| `11844406044` | `brand`·`model_name` 모두 `PRODUCT_LINE_NOT_IN_VALUE` → 근거 없음 |

### Negative inference 금지 (§20)

두 번째 경우에 아무 근거도 제공되지 않지만, **부정 진술은 만들어지지 않습니다.**
`prompt_block()`이 빈 문자열이 되고 `evidence_text()`도 비므로
모델에게 "오디세이"라는 단어 자체가 전달되지 않습니다.
기존 `prompt_block()`의 규칙 문장 —
"A field that is not listed is UNKNOWN. Never say a feature is absent…" — 은 그대로 유지했습니다.
테스트로 못 박았습니다(`test_product_line_absent_from_every_value_stays_unknown`,
`test_a_withheld_identity_is_not_a_negative_claim`).

---

## 6. R3 — DB Identity / 관측성

`ProductFactRepository.identity(digest=False)`를 추가했습니다.

```
{"path": ..., "available": true, "size_bytes": 60170240,
 "modified_at": "2026-08-25T03:30:32+00:00", "sha256": null}
```

- 기본값은 **해시를 계산하지 않습니다**(§22). 답변 경로에서는 호출되지 않습니다.
- `digest=True`일 때만 1 MB씩 스트리밍하여 SHA-256을 계산합니다. **실측 57 ms.**
- 계산 결과가 작업 시작 시 기록한 `cddf3082…ac82ac4c`와 일치함을 확인했습니다.
- 파일이 없으면 예외 없이 `available: false`를 반환합니다.

진단 Export의 `product_facts` 섹션에 `knowledge_db`를 추가했습니다.
**디렉터리는 내보내지 않고 파일명만** 넣습니다 — 기존 테스트
`test_the_export_names_no_machine_or_path`가 지키는 계약이고,
`OJE_PRODUCT_FACTS_DB_PATH`에 절대경로가 설정되면 기계 경로가 새어나가기 때문입니다.
진단은 명시적 호출이므로 이때는 해시를 계산합니다.

기존 문구 `"product_facts.db is not read"`는 그대로 유지했습니다.
DB의 행은 여전히 한 줄도 읽지 않으며, 파일 메타데이터만 봅니다.

---

## 7. 수정 파일

| 파일 | 변경 | 성격 |
|---|---|---|
| `services/product_knowledge_service.py` | +168 | R1·R2·R4 gate. 상수 추가, `FIELD_TOPICS` 분리, 판정 함수 2개 추가, `_judge`/`_exclusion_reason` 인자 확장, `ProductKnowledgeResult`에 `collection_status`·`component_subject` 추가 |
| `repositories/product_fact_repository.py` | +39 | `identity()` 추가. 기존 쿼리·연결 방식 변경 없음 |
| `scripts/export_inquiry_diagnostics.py` | +28 −5 | `knowledge_db` 항목 추가 |
| `tests/test_product_facts_safety_gate_11b.py` | 신규 | 32개 테스트 |

**변경하지 않은 것**: `services/answer_service.py`, `services/hybrid_answer_service.py`,
`services/auto_processing_eligibility_service.py`, `services/learning_evidence_policy.py`,
`answer/prompt_builder.py`, `.env`, DB, ontology.

`_judge`와 `_exclusion_reason`은 `ProductKnowledgeService` 내부 전용이며 외부 호출자가 없음을 확인했습니다.
`prompt_block()` / `evidence_text()` / `as_prompt_line()`은 diff에 등장하지 않습니다 — prompt 구조 미변경(§30).

---

## 8. 신규 테스트

`tests/test_product_facts_safety_gate_11b.py` — **32개 전부 통과**.

**R1 (§11 A~H 전 항목)**
`test_A_collected_listing_answers_static_facts` /
`test_B_dynamic_fact_is_withheld_even_on_a_collected_listing` /
`test_C_uncollected_listing_keeps_its_static_product_facts` /
`test_D_uncollected_listing_withholds_dynamic_facts` /
`test_D2_uncollected_listing_withholds_its_own_terms` /
`test_D3_collected_listing_still_answers_its_own_terms` /
`test_EF_unrecognised_status_fails_closed` (6개 상태 파라미터) /
`test_G_withheld_facts_never_reach_the_prompt_or_evidence` /
`test_H_withheld_facts_do_not_make_an_answer_look_supported`

**R2 (§17)**
구성품 질문 4종 차단 / 자기지시 질문 3종 허용 / 일반 식별 질문 불변 /
구성품의 비식별 사양은 계속 응답 / 판정기 단위 테스트

**R4 (§18~§20)**
세 질문이 세 필드로 분리됨 / "삼성 제품인가요"는 manufacturer로만 응답 /
제품라인이 값에 있으면 근거 사용 / 없으면 UNKNOWN이며 부정 진술 없음

**실제 DB 테스트 3종**
- `test_real_db_package_listing_does_not_lend_its_maker_to_the_set_top_box`
  — 패키지 `11848813000`이 셋톱박스 질문에 `삼성전자`를 주지 않고,
    단독 `11779070305`은 `이노피아테크`를 정상 응답
- `test_real_db_topic_map_only_requests_static_fields`
  — 요청 가능한 필드가 전부 정적임을 기록. 누군가 배송·정책 topic을 추가하면 이 테스트가 실패해 알려줍니다
- `test_real_db_shipped_listings_are_all_currently_collected`
  — 배포된 DB에 미수집 listing이 없음을 기록. 판매종료 포함 artifact가 들어오면 실패로 알려줍니다

fixture는 `certification_number`(topic 매핑이 있는 필드)의 volatility를 파라미터로 바꿔
gate를 실제로 통과시킵니다. 배포 데이터에는 요청 가능한 listing-scoped 필드가 없기 때문이며,
그 사실을 fixture docstring에 적어 두었습니다.

**기존 테스트 기대값은 한 건도 바꾸지 않았습니다.**
진단 Export 테스트 1건이 제 문구 변경 때문에 실패했을 때,
테스트가 지키려는 계약(내용 미노출·경로 미노출)이 옳았으므로
**제 코드를 계약에 맞춰 고쳤습니다**(경로 → 파일명, 원래 문구 복원).

---

## 9. 구버전 DB 회귀 (§25)

변경 전 코드를 `git show HEAD:…`로 꺼내 같은 프로세스에서 나란히 실행하고,
**상품 94개 × 질문 22개 = 2,068건**을 전수 비교했습니다.

| 항목 | 값 |
|---|---|
| 결과 동일 | 1,795 |
| 결과가 달라진 질문 | 273 (전부 식별 질문) |
| 사라진 safe fact | 532 |
| **새로 생긴 safe fact** | **0** |

사라진 532건의 내역은 전부 R4 분리입니다.

| 질문 | 사라진 필드 | 건수 |
|---|---|---|
| 브랜드가 뭔가요? | `manufacturer` / `country_of_origin` | 92 / 86 |
| 제조사가 어디인가요? | `brand` / `country_of_origin` | 88 / 86 |
| 원산지가 어디인가요? | `manufacturer` / `brand` | 92 / 88 |

**질문받은 필드 자체는 100% 보존되었습니다.**

| 질문 | 변경 전 보유 | 변경 후 유지 | 소실 |
|---|---|---|---|
| 브랜드가 뭔가요? → `brand` | 88 | 88 | **0** |
| 제조사가 어디인가요? → `manufacturer` | 92 | 92 | **0** |
| 원산지가 어디인가요? → `country_of_origin` | 86 | 86 | **0** |

비식별 질문(HDMI·베사·인치·스탠드·모델명) 94상품 × 5질문 = 470건은 **변화 0건**입니다.

즉 §25가 요구한 "기존 정상 답변 회귀 = 0"은 충족되었고,
유일한 동작 변화는 §18이 명시적으로 지시한 brand/manufacturer 분리입니다.
이것은 회귀가 아니라 요구된 정책 변경이므로 별도로 보고합니다.

지연 영향도 측정했습니다: 변경 전 중앙값 3.07 ms → 변경 후 3.16 ms (40회, 동일 상품/질문).

---

## 10. 최신 DB READ-ONLY 호환성 (§23)

상품DB 현행 DB(`8fe643c5…`)를 **경로 인자로만** 열어 검사했습니다.
production `data/product_facts.db`는 교체하지 않았습니다.

**(1) 스키마 호환성** — `ProductFactRepository` / `ProductKnowledgeService`가 그대로 읽습니다.
`13074225226`에 7개 질문을 던져 6개 safe fact를 정상 반환했습니다
(`screen_size`, `resolution`, `brand`, `manufacturer`, `certification_number`, `manufacture_date`).

**(2) gate 동작** — 최신 DB의 **임시 사본**에서 해당 listing을 `COLLECTION_FAILED`로 바꾸고
매핑된 필드 하나를 listing 범위로 표시한 뒤 같은 질문을 던졌습니다.

| 질문 | 결과 |
|---|---|
| 화면 크기 / 해상도 / 브랜드 / 제조사 / 출시 | **정적 사실 그대로 유지** |
| 인증번호 (listing 범위로 표시) | **차단** — `COLLECTION_STATUS_NOT_CURRENT` |

정적 사양은 살아남고 listing 범위 사실만 차단된다는 §8 계약이
실제 스키마·실제 데이터에서 그대로 성립함을 확인했습니다.

---

## 11. `13074225226` 검증 (§24)

**요구된 형태로는 검증할 수 없었습니다.** 이유는 §1에 적은 대로,
이 PC의 두 DB 모두에서 이 상품이 `COLLECTION_SUCCESS`이기 때문입니다.
판매종료 상태를 가진 artifact가 이 PC에 없습니다.

대신 수행한 것:

- 두 DB에서 이 상품의 `collection_status`를 직접 조회해 `COLLECTION_SUCCESS`임을 확인
- 최신 DB의 임시 사본에서 이 상품만 `COLLECTION_FAILED`로 바꿔 §10의 검증을 수행
- 원본 두 파일의 SHA-256이 작업 전후 동일함을 확인

판매종료 listing을 포함한 실제 artifact가 반입되면,
`test_real_db_shipped_listings_are_all_currently_collected`가 실패하며 그 사실을 알립니다.
그 시점에 실물 회귀 검증을 다시 수행해야 합니다.

---

## 12. DPS 라우팅 불변 (§26)

`services/answer_service.py`는 **한 줄도 변경하지 않았습니다.**
Phase 11-A에서 확인한 순서가 그대로 유지됩니다.

```
1215행  plan = self.plans.create(...)        ← requires_dps_lookup / requires_order_lookup 확정
1236행  product_knowledge = ...facts_for_inquiry(...)
1327행  if plan.requires_dps_lookup: ...
```

Product Facts 결과가 `plan`으로 되돌아가는 경로가 없으므로 우회는 구조적으로 불가능합니다.
DPS는 실제로 실행하지 않았습니다.

## 13. Missing Item 정책 불변 (§27)

`MISSING_ITEM_REPORT` 차단은 `answer/inquiry_analysis.py`의 `can_generate_answer=False`에서
Product Facts 조회보다 **앞선 단계**에 일어납니다. 해당 코드는 변경하지 않았습니다.
`tests/test_missing_item_manual_only.py`, `tests/test_missing_item_report.py` 통과를 확인했습니다.

## 14. Learning 충돌 불변 (§28)

`services/learning_evidence_policy.py`는 변경하지 않았습니다.
`PRODUCT_FACT_VS_LEARNING_CONFLICT` 정책과 Learning ranking/retrieval 모두 그대로입니다.
`tests/test_learning_authority_and_model_identity.py`,
`tests/test_learning_hardening_golden.py` 통과를 확인했습니다.

## 15. Auto-post 안전성 (§29)

`services/auto_processing_eligibility_service.py`는 변경하지 않았습니다.
기존 `PRODUCT_FACT_NOT_VERIFIED` 차단이 그대로 동작합니다.

이번에 새로 차단되는 사실들이 **검증된 Product Facts처럼 취급되지 않음**을 확인했습니다.
차단된 fact는 `excluded_facts`로 분류되어 `safe_facts`에 들어가지 않으므로

- `has_safe_facts` → `False`
- `supports_question(...)` → `False`
- `covers_all([...])` → `False`
- `prompt_block()` / `evidence_text()` → 빈 문자열

이 되고, 결과적으로 `current_fact_verified`가 서지 않아 자동등록이 막힙니다
(`test_H_withheld_facts_do_not_make_an_answer_look_supported`).

§26~§29 회귀 확인을 위해 관련 테스트 8개 파일을 실행해 **383개 전부 통과**했습니다.

---

## 16. DB 무변경 검증 (§34)

| DB | 작업 전 SHA-256 | 작업 후 SHA-256 | 결과 |
|---|---|---|---|
| 개발 PC `data/product_facts.db` | `cddf3082…ac82ac4c` | `cddf3082…ac82ac4c` | **불변** |
| 상품DB 현행 `data/product_facts.db` | `8fe643c5…4e563f93` | `8fe643c5…4e563f93` | **불변** |

`data/oje_automation.db`도 읽기 전용(`mode=ro` + `PRAGMA query_only=ON`)으로만 접근했습니다.
임시 사본은 scratchpad에만 만들었고 저장소나 `data/`에는 남기지 않았습니다.

---

## 17. 전체 테스트 (§33)

과거 숫자를 재사용하지 않고 이번 작업 시작 시점에 직접 측정했습니다.

| 구분 | passed | failed | skipped | 소요 |
|---|---|---|---|---|
| 변경 전 baseline | **3,494** | 0 | 0 | 20분 00초 |
| 변경 후 | **3,526** | 0 | 0 | 17분 48초 |
| 차이 | **+32** | 0 | 0 | – |

증가한 32개는 이번에 추가한 `tests/test_product_facts_safety_gate_11b.py` 전부입니다.
**기존 테스트 실패 0, 신규 테스트 실패 0** — §33 목표를 충족했습니다.

`data/product_facts.db`가 존재하므로 `@real_db` 테스트도 전부 실행되어 통과했습니다
(skipped 0이 이를 뒷받침합니다).

---

## 18. 잔여 위험

**MEDIUM — R2 보수적 판정으로 인한 커버리지 손실**
구성품 단독 listing에 자기지시 표지 없이 묻는 질문("스탠드 제조사가 어디예요?")은 차단됩니다.
listing이 곧 구성품인지 판별할 신뢰 가능한 신호가 현재 데이터에 없기 때문입니다
(`product_type` 4건 중 3건 오분류, 디스플레이 사실 커버리지 부족).
실제 문의 기준 영향은 0.6% 이내이며 오답이 아니라 미답변입니다.

**MEDIUM — R1 gate가 아직 도달 불가**
`FIELD_TOPICS`가 요청하는 63개 필드가 전부 정적이라 gate는 현재 발동하지 않습니다.
배송·가격·정책 topic이 추가되는 순간 하중을 받게 되며,
그때 정책이 맞는지 다시 검토해야 합니다.

**MEDIUM — 판매종료 artifact로 실물 검증 미완**
§24의 실물 회귀는 해당 artifact가 이 PC에 없어 수행하지 못했습니다.

**LOW — 구성품 단어 목록은 손으로 관리됨**
새 구성품 유형(예: 사운드바)이 패키지에 추가되면 목록을 갱신해야 합니다.

**LOW — `country_of_origin` 값 표기가 제각각**
`한국(베트남, 중국, …)`, `국산`, `베트남산(삼성전자(주))` 등 같은 의미의 표기가 여러 형태입니다.
이번 범위 밖이며 Product Facts 쪽 정규화 과제입니다.

**LOW — product-line 목록이 3개로 한정**
`오디세이`, `스마트모니터`, `무빙스타일`만 인식합니다. `에센셜`, `비즈니스TV` 등은 미대응(미답변).

---

## 19. Phase 11-C 권장 범위

1. **최신 Product Facts artifact 반입** — versioned 파일명 + SHA-256, 기존 파일은 `data/archive/`로 보존,
   `OJE_PRODUCT_FACTS_DB_PATH` 지정. `data/product_facts.db`의 git 추적 정책 결정(57 MB 바이너리).
2. **반입 직후 실물 회귀** — `@real_db` 테스트 전체 재실행.
   `test_real_db_shipped_listings_are_all_currently_collected`가 실패하면
   그것이 판매종료 listing이 들어왔다는 신호이므로, 그 상품으로 §24 검증을 정식 수행.
3. **커버리지 재측정** — Phase 11-A에서 측정한 30.5%가 최신 DB에서 어떻게 변했는지.
4. 그 다음에야 실제 고객 답변 적용 여부를 판단합니다.

Phase 11-C에서도 GPT prompt 구조 변경, auto-post 로직 변경, DPS 연동은 범위 밖으로 두는 것을 권장합니다.

---

## 20. 최종 판정

# PHASE 11-B READY — 최신 Product Facts 반입 전 Safety Gate 보강 완료

판정 근거:

- R1 `collection_status` gate를 중앙 안전 경계 한 곳(`_exclusion_reason`)에 추가했고,
  정적 사양 보존 / listing 범위 사실 차단 / 미지 상태 fail-closed를 모두 테스트로 고정했습니다.
- R2 구성품 주어 gate가 실재하는 오답(패키지 manufacturer `삼성전자` → 셋톱박스 실제 제조사 `이노피아테크`)을
  차단함을 실제 DB로 검증했습니다. 실제 문의 2,772건 기준 영향은 17건(0.6%)이며 전부 의도한 대상이거나 무해합니다.
- R4 brand / manufacturer / country_of_origin 분리와 product-line 근거 요건을 구현했고,
  근거 부재가 부정 진술로 바뀌지 않음을 테스트로 고정했습니다.
- R3 DB fingerprint를 추가해 어떤 artifact로 답하고 있는지 진단에서 확인 가능해졌습니다.
- 구버전 DB 회귀 0(질문받은 필드 100% 보존, 비식별 질문 470건 변화 없음),
  전체 테스트 3,494 → 3,526 passed, 실패 0.
- DPS 라우팅 · Missing Item 정책 · Learning 충돌 정책 · auto-post gate 코드는 한 줄도 변경하지 않았습니다.
- 두 Product Facts DB 모두 SHA-256 불변.

READY는 최신 DB를 반입해도 된다는 뜻이 아니라,
**반입 전에 닫아야 할 gate가 닫혔고 반입 시 검증할 장치가 준비되었다**는 뜻입니다.

다만 §11에 적은 대로 **§24의 실물 판매종료 회귀 검증은 수행하지 못했습니다.**
해당 artifact가 이 PC에 없기 때문이며, 반입 시점에 반드시 다시 수행해야 합니다.
