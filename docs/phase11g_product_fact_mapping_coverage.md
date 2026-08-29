# Phase 11-G — Product Facts 질문 매핑·구어체 활용률 개선

작업일: 2026-08-30 · Product Facts DB 수정 없음 · 서버 PC 미접속

---

## 요약

이미 안전하게 VERIFIED된 사실을 실제 고객 표현과 더 잘 연결했습니다.
안전 게이트는 하나도 완화하지 않았고, 오히려 검증 과정에서 **실제 데이터에 존재하던
subject 오답 경로 하나를 발견해 막았습니다.**

| 항목 | Before | After |
|---|---|---|
| 상품사실 주제로 인식된 문의 | 557 | **649** |
| exact join | 176 | **209** |
| 실제 Product Facts 근거 제공 | 58 | **67** |
| gate 과차단(False Negative) | 13 | **7** |

| 안전 지표 | 결과 |
|---|---|
| 신규 evidence wrong mapping | **0** (11건 전수 감사) |
| cross-product leakage | **0** |
| unsupported claim | **0** |
| DPS bypass | **0** (400건 대조 실험, 라우팅 차이 0) |
| Missing Item unsafe auto-answer | **0** |
| component inheritance error | **0** |
| False Positive | **0** |

---

## 시작 상태

| 항목 | 값 |
|---|---|
| branch | `main` |
| HEAD / origin/main | `2bda0d1` (동일) |
| `git status` | clean |

기존 사용자 변경은 없었습니다.

---

## Phase 11-F 기준선

| 항목 | 값 |
|---|---|
| 실제 문의 replay | 2,772 |
| 상품사실 관련 문의 | 557 |
| exact join | 176 |
| 실제 근거 제공 | 58 (33.0%) |
| 전체 테스트 | 3,526 passed / 0 failed / 0 skipped |

---

## DB Identity

| 항목 | 값 |
|---|---|
| SHA-256 | `e0cdd3639cb4f0c5f9bc3f2d1f3c4caf020deca45b9144590614e4bc6f55a078` **일치** |
| listings | 94 (`COLLECTION_SUCCESS` 93 / `COLLECTION_FAILED` 1) |

DB는 읽기만 했습니다.

---

## FIELD_TOPICS 구조

| 항목 | 확인 결과 |
|---|---|
| 구조 | `(keywords, base_fields, accessory_fields)` 튜플의 순서 있는 목록 |
| 방향 | topic → fields (여러 topic이 동시에 선택되어 fields가 누적) |
| 매칭 | **substring** — 소문자·공백 정규화 후 `keyword in text` |
| 복합질문 | 여러 topic이 각각 매칭되어 후보 field가 합쳐짐 |
| 동의어 처리 위치 | 별도 계층 없이 keyword 목록 안에서 처리 |
| 연결 지점 | `fields_for_question()` → `facts_for_inquiry()` |

**매핑 시스템은 `services/product_knowledge_service.py` 하나뿐**임을 확인했습니다
(`FIELD_TOPICS` / `fields_for_question`를 참조하는 production 파일이 이 파일 하나).
따라서 제3의 매핑 시스템을 새로 만들지 않고 기존 구조만 확장했습니다.

---

## 실제 Field Inventory

최종 DB의 ACTIVE·VERIFIED·미보류 fact를 상품 수 기준으로 조사했습니다(발췌).

| 영역 | field | usable | volatility |
|---|---|---|---|
| 소비전력 | `power_consumption_typical_w` | 20 | STATIC |
| | `power_consumption_max_w` | 2 | STATIC |
| | `power_consumption_dpms_w` | 1 | STATIC |
| 전원 | `power_cable_included` | 38 | STATIC |
| | `power_cable_length_m` | 1 | STATIC |
| 리모컨 | `remote_control_included` | 2 | STATIC |
| 케이블 | `hdmi_cable_included` | 1 | STATIC |
| OTT | `youtube_supported` | 1 | STATIC |
| | `ott_supported` | 2 | STATIC |
| | `ott_supported_services` | **0** | STATIC |
| | `tv_plus` | 21 | STATIC |
| 연결 | `bluetooth_present` / `bluetooth_version` | 1 / 3 | STATIC |
| | `screen_mirroring` | 19 | STATIC |
| | `wireless_display` | 1 | STATIC |
| | `mobile_wireless_connection` | 1 | STATIC |
| 설치 | `installation_method` | 35 | **SEMI_STATIC** |
| | `package_professional_installation` | 1 | STATIC |
| 셋톱박스 | `set_top_box_wifi_present` / `_standard` | 32 / 32 | STATIC |
| | `set_top_box_ott_supported` | 16 | STATIC |
| | `set_top_box_bluetooth_*` | 4 | STATIC |

값도 확인했습니다 — `installation_method = "PROFESSIONAL_TECHNICIAN_REQUIRED"` (35건),
`ott_supported_services`는 **usable 값이 하나도 없음**.

---

## 매핑 공백 원인

Phase 11-F가 지목한 공백을 현재 HEAD에서 재현하고 분류했습니다.

| 질문 | 분류 |
|---|---|
| 전기 얼마나 먹나요? / 전기 많이 먹나요? | **B. synonym 없음** (`소비전력`·`전력`은 이미 매핑) |
| 휴대폰이랑 연결돼요? | **B. phrase 없음** (의미가 여러 개 — §15) |
| 유튜브 되나요? / 넷플릭스 되나요? / OTT | **A. field 미매핑** |
| 리모컨 포함인가요? | **A. field 미매핑** |
| 구성품이 뭐가 들어있나요? | **A + F.** 부분 evidence만 존재 (§18) |
| 셋톱박스 관련 | **F. semantic contract 불명확** (§19–20) |
| 설치는 어떻게 하나요? | **A. field 미매핑** |

D(안전 차단)·E(fact 없음)에 해당하는 항목은 매핑으로 해결하지 않았습니다.

---

## 고객 구어체 Corpus

운영 DB를 read-only로 읽어 상품 사양 어휘가 등장하는 문장을 비식별 추출했습니다
(8자리 이상 숫자가 포함된 문장은 제외, 개인정보 미저장).

**검토 표현 400개** 분류:

| 분류 | 건수 |
|---|---|
| recognized | 167 |
| unrecognized | 147 |
| DPS | 75 |
| Missing Item | 11 |

unrecognized 중 반복 등장한 패턴이 이번 매핑의 근거가 되었습니다 —
특히 `설치는 기사님이 해주시나요?` 계열과 `리모컨도 같이 배송되는지` 계열입니다.

---

## 소비전력

`power_consumption_typical_w`(20)·`max_w`(2)에 근거가 있습니다.

추가한 표현: `전기 얼마나`, `전기 많이`, `전기 요금`, `전기요금`

**`전기` 단독을 키로 쓰지 않았습니다.** 전기 케이블·전기 코드까지 끌어오기 때문입니다.
`power_consumption_dpms_w`(대기전력)는 다른 개념이므로 합치지 않았고 매핑하지 않았습니다.

반대 방향도 함께 고정했습니다 — `전원 케이블` / `전원선` / `파워 케이블`은
`power_cable_included`·`power_cable_length_m`로만 가고 소비전력에는 닿지 않습니다.

---

## 휴대폰 연결

"휴대폰이랑 연결돼요?"는 §15가 지적한 대로 **의미가 하나가 아닙니다.**
Bluetooth·mirroring·wireless display 모두 정당한 해석입니다.

`FIELD_TOPICS`가 topic당 여러 field를 허용하므로, 하나를 추측하지 않고
**연결 관련 fact 전부를 후보로** 올렸습니다.

| 표현 | 후보 field |
|---|---|
| 휴대폰 연결 / 핸드폰 연결 / 스마트폰 연결 / 폰 연결 / 휴대폰 연동 | `bluetooth_present`, `bluetooth_version`, `screen_mirroring`, `wireless_display`, `mobile_wireless_connection` |
| 폰 화면 / 휴대폰 화면 / 핸드폰 화면 | `screen_mirroring`, `mirroring_without_wifi`, `wireless_display` |

그 상품이 실제로 가진 방식만 근거가 되고, 없으면 UNKNOWN입니다.

---

## OTT / YouTube

§16의 "서비스 ≠ 범주"를 field 수준에서 분리했습니다.

| 표현 | field | 근거 |
|---|---|---|
| 유튜브 / youtube | `youtube_supported` | 명시된 서비스 |
| 넷플릭스 / 넷플 | `ott_supported_services` | 서비스명을 담을 수 있는 유일한 field |
| ott / 오티티 | `ott_supported`, `ott_supported_services` | 범주 |
| tv플러스 / 티비플러스 | `tv_plus` | 별개 서비스 |

`ott_supported`가 `youtube_supported`를 대신하지 않고, 그 반대도 아닙니다.
`ott_supported_services`는 현재 usable 값이 0이므로 넷플릭스 질문은 **UNKNOWN**이 정답이며,
매핑은 그 사실을 정직하게 드러냅니다.

---

## 리모컨

§17의 네 의미를 분리했습니다.

| 의미 | 처리 |
|---|---|
| 포함 여부 | `리모컨 포함`, `리모컨 들어`, `리모컨 동봉`, `리모컨도 주`, `리모컨도 같이`, `리모컨 같이 오` → `remote_control_included` |
| 종류/모델 | 해당 field가 DB에 없음 → **매핑하지 않음 (UNKNOWN)** |
| 누락 신고 | 기존 Missing Item 경로 유지, 후보 field 0개 |

**`리모컨` 단독을 키로 쓰지 않았습니다.** 포함 의도를 나타내는 구(句)만 키로 삼아
"리모컨이 안 왔어요"와 경로를 공유하지 않게 했습니다.

---

## 구성품

§18에 따라 **열린 목록 질문은 매핑하지 않았습니다.**

"구성품이 뭐가 들어있나요?" / "박스에 뭐 들어있어요?"는 여전히 UNKNOWN입니다.
DB에 있는 것은 `power_cable_included`(38), `remote_control_included`(2),
`hdmi_cable_included`(1) 같은 **부분 근거**뿐이고,
이것으로 "구성품은 A, B, C입니다"라고 답하면 완전한 목록인 것처럼 오해됩니다.

대신 특정 구성품을 지목한 질문만 답합니다 —
"전원 케이블 포함인가요?", "HDMI 케이블 주나요?", "리모컨 포함인가요?".

---

## Set-top Box — 보류

§20의 6개 조건을 대조한 결과 **production 적용을 보류**했습니다.

| 조건 | 판정 |
|---|---|
| 1. 포함/별도구매/지원/호환 의미를 field 수준에서 구분 | **✕** — 존재하는 것은 `set_top_box_wifi_*`, `set_top_box_ott_*`, `set_top_box_bluetooth_*` 즉 **셋톱박스 자체 사양(D)** 뿐이며 `set_top_box_included` 같은 포함 여부 field가 없음 |
| 2. package/component subject 구분 | **△** — `component_scope`가 `accessory_` 접두사만 인식해 `set_top_box_*`를 본체 사실로 분류 |
| 3. component-specific evidence 존재 | ○ |
| 4. listing identity를 상속하지 않음 | ○ (component gate가 차단) |
| 5. UNKNOWN을 미포함으로 해석하지 않음 | ○ |
| 6. Phase 11-B component gate 통과 | 조건 2 때문에 불확실 |

조건 1과 2가 불명확하므로 §20에 따라 **"설계 선행 필요"로 남깁니다.**
"셋톱박스도 같이 오나요?"는 계속 UNKNOWN이며, 이는 오답보다 낫습니다.

---

## 설치

§21의 구분을 키워드 수준에서 강제했습니다.

| 질문 유형 | 처리 |
|---|---|
| **방법/주체** — `설치 방법`, `설치는 어떻게`, `자가설치`, `기사님이 설치`, `설치기사` | `installation_method`, `package_professional_installation` |
| **일정** — `설치 언제`, `기사님 언제`, `설치 날짜` | 후보 field 0개 → 기존 DPS 라우팅 |

근거: `installation_method = "PROFESSIONAL_TECHNICIAN_REQUIRED"`(35개 상품)가
"기사님이 설치해주시나요?"에 직접 답합니다.

**`설치` 단독을 키로 쓰지 않았습니다.**

`installation_method`는 `SEMI_STATIC_POLICY_FACT`이므로, 이 매핑으로
**Phase 11-B의 collection_status gate가 처음으로 실제 하중을 받게 되었습니다.**
판매종료 listing에서 이 질문이 차단되는지 별도 테스트로 고정했습니다.

---

## 적용한 Mapping

| # | 표현 | target field | 근거(usable) |
|---|---|---|---|
| 1 | 전기 얼마나 / 전기 많이 / 전기 요금 | `power_consumption_typical_w`, `_max_w` | 20 / 2 |
| 2 | 전원 케이블 / 전원선 / 파워 케이블 | `power_cable_included`, `power_cable_length_m` | 38 / 1 |
| 3 | hdmi 케이블 | `hdmi_cable_included` | 1 |
| 4 | 리모컨 포함·들어·동봉·같이 (구 8종) | `remote_control_included` | 2 |
| 5 | 휴대폰/핸드폰/스마트폰/폰 연결·연동 | 연결 관련 5개 field | 1~19 |
| 6 | 폰/휴대폰/핸드폰/스마트폰 화면 | `screen_mirroring` 외 2 | 19 |
| 7 | 유튜브 / youtube | `youtube_supported` | 1 |
| 8 | 넷플릭스 / 넷플 | `ott_supported_services` | 0 (정직한 UNKNOWN) |
| 9 | ott / 오티티 | `ott_supported`, `ott_supported_services` | 2 |
| 10 | tv플러스 / 티비플러스 | `tv_plus` | 21 |
| 11 | 설치 방법/방식/자가설치/기사 설치 (구 15종) | `installation_method`, `package_professional_installation` | 35 / 1 |

추가로 **subject 안전 수정 1건** — `IDENTITY_FIELDS`에 `model_name`·`model_code`·`part_number`를 넣고
`COMPONENT_TERMS`에 `리모컨`·`리모콘`을 추가했습니다(아래 참조).

---

## 적용하지 않은 Mapping과 이유

| 대상 | 이유 |
|---|---|
| `set_top_box_*` 전체 | §20 조건 1·2 미충족. 포함 여부 field 부재, component scope 설계 선행 필요 |
| 구성품 열린 목록 질문 | §18. 부분 evidence를 완전한 목록처럼 제시할 위험 |
| 리모컨 종류/모델 | 해당 field가 DB에 없음 |
| `installation_fee_applies` | 설치비는 정책/주문 영역. `설치비` 질문은 DPS·Template 경계 |
| 디즈니플러스·웨이브·티빙 | 해당 서비스 field 없음 → UNKNOWN 유지 |
| DP 케이블 포함 | `displayport_cable_included` 같은 field 없음 |
| `무선으로 연결되나요?` | LEVEL 3 표현. 주변 단어 없이 광범위 매핑 금지(§13) |

---

## 검증 중 발견해 고친 실제 결함

**"리모컨 모델명이 뭐예요?" 가 TV의 `model_name`을 반환했습니다.**

`모델명` topic이 `model_name`·`model_code`·`part_number`를 요청하는데,
`리모컨`이 `COMPONENT_TERMS`에 없어 component subject gate가 발화하지 않았습니다.
**옳은 field, 틀린 subject**입니다.

수정: `COMPONENT_TERMS`에 `리모컨`·`리모콘` 추가,
`IDENTITY_FIELDS`에 `model_name`·`model_code`·`part_number` 추가.

이 수정으로 실제 문의 2건이 근거를 잃었고, **둘 다 잃는 것이 정답**이었습니다.

| id | 질문 성격 | 이전 동작 |
|---|---|---|
| 1892 | TV와 함께 산 **무빙스탠드의 모델명**을 물음 | TV의 `model_name`을 답할 뻔 |
| 1899 | 모니터+스탠드 **패키지 상품 코드**를 물음 | 본체 `model_code`를 답할 뻔 |

일반 질문("모델명이 뭐예요?")은 그대로 답합니다.

---

## Synthetic Matrix

**101개 질문** — 소비전력 12, 휴대폰/연결 16, OTT 15, 리모컨 12, 구성품 8,
설치 14, 셋톱박스 4, 기존 매핑 회귀 20.

각 질문마다 기대 라우팅(PF / PF_BLOCKED / PF_NO_DATA / DPS / MISS / UNK)과
기대 field를 정의하고, 매핑만이 아니라 **그 field를 실제로 가진 상품에서 gate를 통과하는지**까지 확인했습니다.

```
질문 수 : 101
실패    : 0
```

측정 과정에서 기대값 5건을 정정했습니다 — 모두 제 기대가 틀렸던 경우입니다.

| 질문 | 정정 내용 |
|---|---|
| 리모컨도 같이 배송되나요? | `배송` 때문에 배송 문의로 분류 → DPS 우선이 맞음 |
| 설치기사 방문하나요? | `방문` 때문에 방문 문의로 분류 → DPS 우선이 맞음 |
| 리모컨 모델명이 뭐예요? | 매핑은 되지만 component gate가 전부 차단 → `PF_BLOCKED` |
| 넷플릭스 되나요? | field는 맞지만 DB에 usable 값 0 → `PF_NO_DATA` |
| 리모컨 없이 왔는데요 / 주문 상태 알려주세요 | 기존 분류기가 잡지 못하는 표현. Product Facts는 근거를 주지 않아 안전 |

---

## Positive / Negative Pair

**12쌍**을 명시적으로 검증했고 **topic bleed 0건**입니다.

| Positive | Negative | 결과 |
|---|---|---|
| 소비전력이 얼마인가요? | 전원 케이블 포함인가요? | bleed 없음 |
| 전기 얼마나 먹나요? | 전원선 들어있나요? | bleed 없음 |
| 리모컨 포함인가요? | 리모컨이 안 왔어요 | 후보 field 0개 |
| 유튜브 되나요? | OTT 볼 수 있나요? | 서로 다른 field |
| 넷플릭스 되나요? | 유튜브 되나요? | 서로 다른 field |
| 설치 방법 알려주세요 | 설치 언제 오나요? | 후보 field 0개 |
| 설치는 어떻게 하나요? | 기사님 언제 오나요? | 후보 field 0개 |
| 휴대폰이랑 연결돼요? | 벽걸이 설치 가능한가요? | bleed 없음 |

테스트 파일에도 같은 쌍을 고정했습니다.

---

## 복합질문

Phase 11-F의 복합질문 15개를 그대로 재실행했고, sub-question 독립 처리가 유지됩니다.
새 매핑이 추가된 뒤에도 하나의 fact가 다른 sub-question을 채우는 사례는 없습니다.

실제 문의에서도 확인되었습니다 — 신규 evidence 11건 중 4건이 복합문의였고
(예: A/S·설치 주체·브라켓 호환·설치예정일·카드할인을 한 번에 묻는 문의),
`installation_method`는 **설치 주체 sub-question에만** 답하고
설치예정일은 계속 DPS로 갑니다.

---

## 실제 문의 Replay

동일 corpus 2,772건, 동일 denominator 정의로 Before/After를 측정했습니다.
Before는 `git show HEAD:services/product_knowledge_service.py`로 꺼낸
Phase 11-F 코드를 같은 프로세스에서 실행해 재현했습니다.

| 항목 | Before | After | 증감 |
|---|---|---|---|
| 전체 문의 | 2,772 | 2,772 | – |
| 상품사실 주제 문의 | 557 | **649** | +92 |
| exact join | 176 | **209** | +33 |
| 실제 evidence 제공 | 58 | **67** | **+9** |

---

## 신규 Evidence 감사

evidence를 새로 얻은 문의 **11건 전수**를 감사했습니다.

| 검사 항목 | 위반 |
|---|---|
| 올바른 매핑 | 0 |
| 올바른 상품(`same_product`) | 0 |
| VERIFIED | 0 |
| resolution에 CONFLICT/NEEDS_REVIEW 없음 | 0 |
| provenance ≥ 1 | 0 |

11건 모두 `installation_method = PROFESSIONAL_TECHNICIAN_REQUIRED`
(일부는 `package_professional_installation = YES` 동반)이며,
질문은 전부 "자가설치 가능한가요?", "기사님이 설치해주시나요?",
"혼자 설치할 수 있나요? 설치 방법도 알려주세요" 계열이었습니다.
**값이 질문에 직접 답합니다.**

evidence를 잃은 2건은 위에 적은 대로 **의도한 안전 차단**입니다.
순증 = 11 − 2 = **+9**.

---

## 활용률 Before / After

| 지표 | Before | After |
|---|---|---|
| evidence 제공 문의 | 58 | **67 (+15.5%)** |
| exact join 대비 비율 | 33.0% | 32.1% |

**비율이 소폭 내려간 것을 숨기지 않습니다.** 분자(58→67)보다 분모(176→209)가
더 크게 늘었기 때문입니다. 분모가 늘어난 이유는 이전에는 "상품사실 질문"으로
인식조차 되지 않던 표현 92건이 이제 인식되기 때문이며, 그 자체가 개선입니다.
데이터가 채워지면 같은 분모에서 분자가 오릅니다.

절대 수치로는 evidence가 **+9건(+15.5%)** 늘었습니다.

---

## Safety Regression

Phase 11-F의 검증을 동일 하네스로 재실행했습니다.

| 지표 | Phase 11-F | **Phase 11-G** |
|---|---|---|
| cross-product leakage | 0 | **0** |
| unsupported claim | 0 | **0** |
| provenance 누락 | 0 | **0** |
| False Positive | 0 | **0** |
| gate 과차단(False Negative) | 13 | **7** |
| positive scenario | 34 | **40** |
| safe auto 후보 | 34 | **40** |
| 총 시나리오 | 246 | **252** |

**라우팅 불변**: 실제 문의 400건을 Product Facts 정상/완전 비활성 두 조건으로 분석해
**라우팅 결정 차이 0건**, 그리고 **Phase 11-F 대비 차이도 0건**입니다.
DPS bypass·Missing Item 정책 모두 그대로입니다.

`false_positive` 항목에 3건이 표시되지만 Phase 11-F에서 규명한 것과 **동일한 계측 오류**입니다 —
누락문의를 조회 계층에서 직접 호출한 결과이며, 실제 파이프라인은
`can_generate_answer=False`로 답변 생성 자체를 차단합니다.

---

## 성능

같은 프로세스에서 Before(11-F 코드)와 After(11-G 코드)를 나란히 측정했습니다.

| 구간 | n | median | p95 | max |
|---|---|---|---|---|
| Before (11-F 코드) | 240 | 3.18 ms | 4.16 ms | 6.66 ms |
| **After (11-G 코드)** | 240 | **3.13 ms** | **3.92 ms** | **4.85 ms** |

**회귀 없음** — 차이는 측정 잡음 범위입니다(Phase 11-F 보고치 median 3.26 ms와도 같은 수준).

키워드 스캔 자체의 비용도 따로 쟀습니다.

| `fields_for_question()` | median | p95 |
|---|---|---|
| Before | 22.9 µs | 37.0 µs |
| After | 28.0 µs | 50.8 µs |

+5 µs로, 3 ms대인 조회 전체 비용에 묻히는 수준입니다.
전체 ontology를 스캔하는 방식이 아니라 기존 keyword 목록에 항목을 더한 것이기 때문입니다.

**주의**: 중간 측정에서 median 4.30 ms가 나온 적이 있으나
전체 테스트를 동시 실행하던 중이라 경합의 영향이었습니다. 위 수치가 유효합니다.

---

## 전체 테스트

**§41 Product Facts 관련 21개 파일**

```
837 passed in 223.41s (0:03:43)
```

repository / `ProductKnowledgeService` / topic mapping / AnswerService·Hybrid /
AnswerValidator / `AutoProcessingEligibilityService` / Learning conflict /
collection_status / component subject / brand·manufacturer / Missing Item /
DPS routing / stale DPS / real_db / E2E — **0 failed, 0 skipped**.

**§42 전체**

```
3570 passed in 1107.09s (0:18:27)
```

| 시점 | passed | failed | skipped |
|---|---|---|---|
| Phase 11-F baseline | 3,526 | 0 | 0 |
| **Phase 11-G** | **3,570** | **0** | **0** |

증가한 44건은 이번에 추가한
`tests/test_product_fact_question_mapping_11g.py` 43건과
`test_product_facts_safety_gate_11b.py`의 신규 1건입니다.
테스트를 삭제·skip·xfail 처리한 항목은 없습니다.

---

## DB 무변경

| 항목 | 값 |
|---|---|
| SHA-256 | `e0cdd363…6f55a078` **작업 전후 동일** |
| size / mtime | 142,131,200 / 2026-08-29 23:42:01 **동일** |
| WAL / SHM / journal | **없음** |

---

## 잔여 공백

**MEDIUM — Set-top Box 의미 영역**
`set_top_box_*` 사실이 32개 상품에 있으나 §20 조건 미충족으로 보류했습니다.
포함 여부 field 부재와 `component_scope`의 `accessory_` 접두사 한정이 선결 과제입니다.

**MEDIUM — 구성품 열린 목록**
부분 evidence만 존재해 "무엇이 들어있나요?"에는 답하지 않습니다.
완전한 구성품 목록이 DB에 갖춰지기 전까지는 이 상태가 맞습니다.

**LOW — 서비스별 field 부재**
넷플릭스·디즈니플러스·웨이브·티빙은 해당 field가 없어 UNKNOWN입니다.
`ott_supported_services`에 값이 채워지면 넷플릭스 질문은 자동으로 답 가능해집니다.

**LOW — 기존 분류기 표현 공백 2종**
`리모컨 없이 왔는데요`가 Missing Item으로 분류되지 않고,
`주문 상태 알려주세요`가 DPS로 분류되지 않습니다.
둘 다 Product Facts와 무관한 기존 분류기 동작이며 근거를 제공하지 않아 안전합니다.
별도 과제로 남깁니다.

**LOW — `product_id` 부재**
`CUSTOMER_INQUIRY` 1,110건은 상품 식별자가 없어 조회 자체가 불가능합니다.
상품사실 문의 649건 중 exact join이 209건에 그치는 주된 이유입니다.

---

## 최종 지표

| # | 항목 | 값 |
|---|---|---|
| 1 | 실제 고객 표현 검토 수 | **400** |
| 2 | 신규/개선 synonym 수 | **약 60개 표현** (11개 topic 항목) |
| 3 | 신규 field/topic mapping 수 | **11개 topic**, 신규 연결 field 14종 |
| 4 | 적용 보류 mapping 수 | **7개 영역** (set_top_box, 구성품 목록, 리모컨 종류, 설치비, 서비스 3종, DP 케이블, 무선 일반) |
| 5 | synthetic question 수 | **101** |
| 6 | positive/negative pair 수 | **12쌍** (테스트에 고정) |
| 7 | 복합질문 수 | **15** |
| 8 | 실제 inquiry replay 수 | **2,772** |
| 9 | 상품사실 inquiry 수 | 557 → **649** |
| 10 | exact join 수 | 176 → **209** |
| 11 | Before actual evidence | **58** |
| 12 | After actual evidence | **67** |
| 13 | Before evidence rate | 33.0% (58/176) |
| 14 | After evidence rate | 32.1% (67/209) |
| 15 | 신규 evidence 수 | **11** (손실 2 → 순증 9) |
| 16 | 신규 evidence wrong mapping | **0** |
| 17 | cross-product leakage | **0** |
| 18 | unsupported claim | **0** |
| 19 | DPS bypass | **0** |
| 20 | Missing Item unsafe auto-answer | **0** |
| 21 | component inheritance error | **0** |
| 22 | False Positive | **0** |
| 23 | safety-related False Negative | **0** (매핑 공백 7건은 별도) |
| 24 | mapping coverage gap | **7** (11-F의 12에서 감소) |
| 25 | latency median / p95 / max | **3.13 / 3.92 / 4.85 ms** |
| 26 | 전체 tests passed / failed / skipped | **3,570 / 0 / 0** |

---

## 최종 판정

# PHASE 11-G CONDITIONAL READY — 안전한 질문 매핑 개선 완료, 일부 의미 영역 설계 필요

§48의 READY 조건을 대조합니다.

| 조건 | 결과 |
|---|---|
| evidence coverage 실제 상승 | ○ 58 → 67 (+15.5%) |
| 신규 evidence wrong mapping = 0 | ○ 11건 전수 감사 |
| cross-product leakage = 0 | ○ |
| unsupported claim = 0 | ○ |
| DPS bypass = 0 | ○ 400건 대조, 라우팅 차이 0 |
| Missing Item unsafe auto-answer = 0 | ○ |
| component inheritance error = 0 | ○ (오히려 기존 결함 1건 제거) |
| False Positive = 0 | ○ |
| 기존 Safety Gate 완화 없음 | ○ 완화 0, 강화 1 |
| 전체 테스트 0 failed / 0 skipped | ○ 3,570 passed |
| DB hash 동일 | ○ |

**모든 READY 조건을 충족했습니다.**

그럼에도 §49에 따라 `CONDITIONAL READY`로 판정하는 이유는,
**set-top box 의미 영역을 의도적으로 보류**했기 때문입니다.
32개 상품에 셋톱박스 고유 사실이 있으나, 포함 여부를 표현하는 field가 없고
`component_scope`가 `accessory_` 접두사만 인식해 §20의 조건 1·2를 만족하지 못합니다.
이 영역은 매핑이 아니라 설계가 선행되어야 하며, 그때까지 UNKNOWN이 정답입니다.

사용 가능 상태이며, 잔여 공백은 후속 개선 대상으로 분리합니다.
