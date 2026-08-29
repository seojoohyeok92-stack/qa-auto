# Phase 11-D — 최종 Product Facts DB 실사용 전환

작업일: 2026-08-29 · **DB 전환 완료** · Git commit/push 없음 · 서버 배포 없음

> **결론을 먼저 적습니다.**
> `data/product_facts.db`를 최종 artifact(`e0cdd363…`)의 byte-identical 복사본으로 전환했습니다.
> 구버전은 `data/product_facts_before_phase11d_20260829T143917Z.db`로 백업했고 SHA-256 일치를 확인했습니다.
> 기본 실행 경로가 실제로 새 DB를 읽는 것을 runtime fingerprint로 확인했으며,
> R1·R2·R4가 기본 경로에서 정상 동작합니다.
> Retrieval Matrix 3,196건에서 **WRONG 0 / UNSUPPORTED_BUT_ANSWERED 0**입니다.

---

## 1. 시작 상태

| 항목 | 값 |
|---|---|
| branch | `main` |
| HEAD | `3950e8b` |
| 기존 변경 | Phase 11-A/B/C의 3개 수정 파일 + 4개 문서 + 1개 신규 테스트 (모두 보존) |

| DB | size | mtime | SHA-256 |
|---|---|---|---|
| `data/product_facts.db` (구버전) | 60,170,240 | 2026-08-25 12:30:32 | `cddf3082…ac82ac4c` |
| `data/product_facts_final.db` | 142,131,200 | 2026-08-29 02:20:20 | `e0cdd363…6f55a078` |

---

## 2. Final DB 재확인

| 검사 | 결과 |
|---|---|
| SHA-256 == `e0cdd3639cb4f0c5f9bc3f2d1f3c4caf020deca45b9144590614e4bc6f55a078` | **일치** |
| `PRAGMA integrity_check` | **ok** |
| listings | **94** |
| collection_status | **`COLLECTION_SUCCESS` 93 / `COLLECTION_FAILED` 1** |
| `13074225226` | **`COLLECTION_FAILED`** |

§5의 기대값과 모두 일치하여 전환을 진행했습니다.

---

## 3. 구 DB Backup

이 저장소의 백업 convention은 `data/` 아래 평면 배치입니다
(`data/oje_automation_before_rollback_20260813.db`,
`data/oje_automation_pre_dps_session_monitor_20260810_112326.db`).
`data/archive/`는 존재하지 않으므로 기존 convention을 따랐습니다.

```
data/product_facts_before_phase11d_20260829T143917Z.db
```

| 검사 | 결과 |
|---|---|
| 원본 SHA-256 | `cddf3082…ac82ac4c` |
| 백업 SHA-256 | `cddf3082…ac82ac4c` |
| **동일 여부** | **일치** |
| 크기 | 60,170,240 (동일) |

원본은 이 시점에 삭제하지 않았고, 이후 §8에서 파일 단위로 덮어썼습니다.

---

## 4. Tripwire 테스트 수정

**대상**: `tests/test_product_facts_safety_gate_11b.py::test_real_db_shipped_listings_are_all_currently_collected`

**기존 의도**: Phase 11-B에서 이 테스트를 만든 이유는 docstring에 적혀 있습니다 —
"when a newer artifact arrives with a delisted listing, this will fail and say so out loud."
당시 배포 DB가 94/94였다는 사실을 기록하고, 판매종료 listing이 들어오면 알리려는 감지선이었습니다.

**왜 전제가 깨졌는가**: 새 최종 DB에 `COLLECTION_FAILED` listing이 실제로 들어왔습니다.
감지선이 예정대로 발화한 것이며, 결함이 아닙니다.
다만 `assert statuses == {"COLLECTION_SUCCESS"}`는 "운영 DB는 항상 전부 수집 성공이어야 한다"를 뜻하는데,
이는 **애초에 안전계약이 아니었습니다.**

**새 테스트가 검증하는 계약** (`test_real_db_uncollected_listing_is_actually_gated`):

배포 DB의 모든 non-success listing을 순회하며 두 가지를 단언합니다.

1. 그 listing에서 사용 가능(usable)한 fact는 **전부 `STATIC_PRODUCT_FACT`**여야 한다
   — 즉 listing의 *제안 조건*은 하나도 살아남지 못한다
2. 그 listing의 VERIFIED `SEMI_STATIC_POLICY_FACT`는 **전부 `COLLECTION_STATUS_NOT_CURRENT`** 사유로 차단돼야 한다
   — 다른 조건에 우연히 걸린 것이 아니라 collection gate가 실제로 발화했음을 요구

**느슨해진 것이 아니라 강해졌습니다.** 기존 테스트는 상태 분포만 봤고,
새 테스트는 그 상태가 실제 답변 경로에 어떤 효과를 내는지를 봅니다.
non-success listing이 없는 DB에서는 순회 대상이 없어 조용히 통과하며,
gate 자체는 fixture 기반 테스트 30여 건이 계속 검증합니다.

---

## 5. Absent-field 테스트 수정

**대상**: `tests/test_product_facts_b5.py::test_real_db_absent_field_stays_absent`

**기존 의도**: "커버리지 공백은 usable fact가 되지 않는다"
— 없는 사실을 부정 진술로 바꾸지 않는다는 계약입니다.

**왜 전제가 깨졌는가**: 이 테스트는 상품 `10194603339`에 verified HDMI 포트 수가 **없다**는
구체적 사실을 하드코딩했습니다. 새 DB는 그 값을 수집했습니다.

| DB | 결과 |
|---|---|
| 구버전 | `hdmi_present=YES`, `hdmi_version=2.0` (port count 없음) |
| 신규 | **`hdmi_port_count=1`** 추가 — 근거 `IMAGE_OCR: "HDMI 1개"` |

근거가 확실한 데이터 개선이며 안전 규칙 위반이 아닙니다.
§9의 지적대로 **특정 DB의 우연한 결측을 영구 계약처럼 사용한 것**이 문제였습니다.

**새 테스트가 검증하는 계약**: 공백을 이름으로 박지 않고 **실행 시점에 찾습니다.**
배포 DB의 94개 상품 × 4개 대표 field를 훑어 실제로 비어 있는 조합만 골라내고,
그 각각에 대해 근거문·prompt에 `없습니다` / `미지원` / `지원하지` / `not supported`가
등장하지 않음을 단언합니다. 검사 대상이 하나도 없으면 그것도 실패로 처리합니다.

계약(`공백 → UNKNOWN, 부정 진술 금지`)은 그대로이고, 그것을 확인하는 방식만
DB 버전에 흔들리지 않게 바꿨습니다. 동일 계약의 fixture 기반 테스트
(`test_D_missing_field_never_becomes_a_negative_claim`)는 손대지 않았습니다.

---

## 6. DPS 날짜 테스트 4건

**Product Facts와 무관한 기존 독립 결함**이며, §11의 허용 범위에서 **테스트 fixture만** 수정했습니다.
`is_schedule_stale()` 등 production DPS 로직은 한 줄도 건드리지 않았습니다.

**증상**: 네 테스트가 DPS 설치/배송 예정일을 `2026-08-28`로 하드코딩합니다.
`dps/dates.py::is_schedule_stale()`은 예정일을 문의의 `created_at`(UTC)과 비교하므로,
UTC 날짜가 2026-08-29로 넘어간 순간(KST 09:00) 예정일이 과거가 되어
`REVIEW_REQUIRED`로 떨어졌습니다.

**수정**: 각 파일에 실행 시점 기준 상대 날짜를 도입했습니다.

```python
UPCOMING_DATE = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
UPCOMING_DATE_KR = korean_date(UPCOMING_DATE)
```

한국어 렌더링은 production의 `answer/answer_format.py::korean_date()`를 그대로 재사용해
"그 날짜가 답변에 도달했는가"라는 단언의 의미를 유지했습니다.

| 파일 | 치환 |
|---|---|
| `tests/test_atomic_answer_completeness.py` | fixture 2곳 + 한국어 단언 1곳 |
| `tests/test_atomic_draft_composition.py` | fixture 2곳 |
| `tests/test_delivery_pipeline_e2e_dps.py` | fixture 8곳 + 한국어 단언 2곳 |
| `tests/test_golden_auto_post_core_e2e.py` | `RecordingDps` 기본값 1곳 |

**기대값은 낮추지 않았습니다.** `SAFE`를 `REVIEW_REQUIRED`로 바꾼 것이 아니라,
"예정일이 아직 오지 않았다"는 fixture의 원래 의도를 시간에 독립적으로 표현했을 뿐입니다.
`2026-08-26`을 쓰는 일정 변경 테스트처럼 다른 의미를 가진 날짜는 그대로 두었습니다.
스테일을 의도적으로 검증하는 테스트가 이 4개 파일에 없음을 먼저 확인했습니다.

**결과**: 4개 파일 **164 passed**.

---

## 7. 전환 전 검증

기본 DB를 교체하기 전에, 수정한 테스트를 **양쪽 DB**로 실행했습니다.

| 조건 | 결과 |
|---|---|
| 기본 경로(구버전 DB) | **107 passed** |
| Final DB 경로 주입 | **107 passed** |

두 버전 모두에서 통과하므로, 수정한 테스트가 특정 DB에 맞춰 조정된 것이 아님을 확인했습니다.

---

## 8. DB 전환

```
cp -f data/product_facts_final.db data/product_facts.db
```

**파일 단위 복사만 사용**했습니다. SQL export/import, 변환, 재구성은 하지 않았습니다.

---

## 9. SHA-256 검증

| 파일 | SHA-256 | 판정 |
|---|---|---|
| `data/product_facts.db` (신규 기본) | `e0cdd3639cb4f0c5f9bc3f2d1f3c4caf020deca45b9144590614e4bc6f55a078` | **기대값 일치** |
| `data/product_facts_final.db` | 동일 | **byte-identical** |
| `data/product_facts_before_phase11d_20260829T143917Z.db` | `cddf3082…ac82ac4c` | **구버전 SHA 유지** |

전환 후 DB 상태:

| 항목 | 값 |
|---|---|
| `PRAGMA integrity_check` | **ok** |
| listings | 94 |
| collection_status | `COLLECTION_SUCCESS` 93 / `COLLECTION_FAILED` 1 |
| `13074225226` | `COLLECTION_FAILED` |

---

## 10. 기본 Repository 검증

경로 주입이 아니라 **override 없는 기본 초기화**로 확인했습니다.

| 항목 | 값 |
|---|---|
| `.env`의 `OJE_PRODUCT_FACTS_DB_PATH` | **설정 없음** |
| 프로세스 환경변수 | `None` |
| `get_product_facts_path()` | `data\product_facts.db` |
| `ProductFactRepository().identity()` path | `data\product_facts.db` |
| size | 142,131,200 |
| **runtime SHA-256** | **`e0cdd363…6f55a078` (기대값 일치)** |
| `ProductKnowledgeService()` 저장소 경로 | `data\product_facts.db` |
| runtime에서 본 collection_status | `COLLECTION_FAILED` 1 / `COLLECTION_SUCCESS` 93 |

**"파일은 교체했는데 runtime은 다른 DB를 읽는 상태"가 없음을 확인했습니다.**

---

## 11. R1 — 기본 경로 실검증

`13074225226` (`COLLECTION_FAILED`)의 ACTIVE fact 전체:

| volatility | 판정 |
|---|---|
| `DYNAMIC_LISTING_FACT` 8건 | 전부 `VOLATILE_LISTING_FACT` (예: `availability`, `listing_price`, `discount_ratio_percent`) |
| `SEMI_STATIC_POLICY_FACT` 9건 | 전부 **`COLLECTION_STATUS_NOT_CURRENT`** (예: `arrival_guarantee`, `delivery_company`, `delivery_fee`) |
| `STATIC_PRODUCT_FACT` | **USABLE 18** / `SUPERSEDED_BY_LATER_RUN` 36 / `VERIFICATION_NEEDS_REVIEW` 6 |

판매종료 listing이 `availability=IN_STOCK`, `product_status=SALE`을 VERIFIED로 들고 있음에도
그 값들이 답변 근거로 나가지 않으며, 화면·포트 같은 정적 사양은 그대로 사용 가능합니다.

---

## 12. R2 — 기본 경로 실검증

| 상품 | 질문 | 결과 |
|---|---|---|
| `11848813000` (TV+STB 패키지) | 셋톱박스도 삼성 제품인가요? | **차단** `COMPONENT_SUBJECT_UNRESOLVED` |
| `11848813000` | 스탠드 제조사가 삼성인가요? | **차단** 동일 |
| `9866761076` (무빙스탠드 패키지) | 같이 오는 스탠드도 삼성 제품인가요? | **차단** 동일 |
| `11779070305` (STB 단독) | 이 셋톱박스 제조사는 어디예요? | 허용 — `manufacturer=이노피아테크` |
| `11779070305` | 이 셋톱박스 브랜드가 어디예요? | 허용 — `brand=SHAKS` |

listing-level brand/manufacturer 상속 **0건**.

---

## 13. R4 — 기본 경로 실검증

| 상품 | 질문 | 요청 필드 | 답변 |
|---|---|---|---|
| `12601323000` | 브랜드가 뭐예요? | `brand` | 삼성 |
| `12601323000` | 제조사가 어디예요? | `manufacturer` | 삼성전자 |
| `12601323000` | 삼성 제품인가요? | **`manufacturer`만** | 삼성전자 |
| **`12101311850`** | **삼성 제품인가요?** | **`manufacturer`만** | **(주)오제플러스** |
| `12101311850` | 브랜드가 뭐예요? | `brand` | 오베닉 |
| `11844406044` | 오디세이 제품인가요? | `brand`,`model_name` | **UNKNOWN** — `prompt_block()`·`evidence_text()` 모두 빈 문자열 |

`12101311850`은 brand가 `오베닉`이므로 brand로 답했다면 오답이었을 것입니다.
근거 부재가 부정 진술로 바뀌지 않는 것도 확인했습니다.

---

## 14. @real_db 테스트

새 기본 DB에서 Product Facts 3개 파일(@real_db 포함) 실행: **107 passed / 0 failed**

수정한 tripwire 테스트가 이제 실제로 판매종료 listing을 순회하며 gate 발화를 검증합니다.

---

## 15. Product Facts 관련 전체 테스트

`ProductFactRepository` / `ProductKnowledgeService` / product fact guard / Learning conflict /
auto-post gate / pre-generation gate / answer generation policy / missing item / diagnostic export
관련 13개 파일: **539 passed / 0 failed**

---

## 16. Retrieval Matrix (기본 경로 재검증)

상품 **94개** × 질문 **34종** = **3,196건**

| 판정 | Phase 11-C (경로 주입) | **Phase 11-D (기본 경로)** |
|---|---|---|
| CORRECT | 1,535 | **1,535** |
| SAFE_UNKNOWN | 1,661 | **1,661** |
| **WRONG** | 0 | **0** |
| **UNSUPPORTED_BUT_ANSWERED** | 0 | **0** |

경로 주입과 기본 경로 결과가 **완전히 동일**합니다.

SAFE_UNKNOWN 사유: `NO_FACT_FOR_TOPIC` 1,057 / `COMPONENT_SUBJECT_UNRESOLVED` 181 /
`PRODUCT_LINE_NOT_IN_VALUE` 167 / `VERIFICATION_NEEDS_REVIEW` 161 / `SUPERSEDED_BY_LATER_RUN` 95.

전환 전 구버전 DB 기준 CORRECT 1,258 대비 **+277건**이 답변 가능해졌습니다.

---

## 17. DPS Routing 불변

`services/answer_service.py`는 이번에도 변경하지 않았습니다.
`plan`(1215행)이 Product Facts 조회(1236행)보다 먼저 확정되고 역방향 경로가 없으므로,
DB 교체가 `requires_order_lookup` / `requires_dps_lookup` 결정을 바꿀 수 없습니다.

DPS 관련 테스트 4개 파일 164 passed(§6)와 §15의 539 passed로 확인했습니다.
실제 DPS는 실행하지 않았습니다.

---

## 18. Missing Item 불변

| 문의 | `is_missing_item_report` |
|---|---|
| 리모컨이 안 왔어요 | **True** |
| 스탠드가 누락됐어요 | **True** |
| 구성품이 빠졌어요 | **True** |
| 구성품이 안 왔어요 | **True** |

새 DB에 `remote_control_included="YES"`가 상품 2개(`10198648691`, `11745748916`)에 있지만,
이 field는 `FIELD_TOPICS`에 매핑돼 있지 않아 **조회 자체가 일어나지 않습니다**
(`unavailable_reason=NO_PRODUCT_FACT_TOPIC`).
게다가 `MISSING_ITEM_REPORT` 차단은 Product Facts 조회보다 앞선 분석 단계에서 일어납니다.

관련 테스트 **92 passed**.

---

## 19. Learning 불변

`services/learning_evidence_policy.py`는 변경하지 않았습니다.
`PRODUCT_FACT_VS_LEARNING_CONFLICT` 계약이 그대로이며,
`test_learning_authority_and_model_identity.py`·`test_learning_hardening_golden.py`가 §15에 포함되어 통과했습니다.

---

## 20. Auto-post 안전성

`services/auto_processing_eligibility_service.py`는 변경하지 않았습니다.
새 DB의 `NEEDS_REVIEW`(853) / `CONFLICT`(136) 및 non-current listing의 unsafe fact는
중앙 판정에서 `excluded_facts`로 분류되어 `safe_facts`에 들어가지 않으므로
`has_safe_facts` → False, `prompt_block()` → 빈 문자열이 되고
기존 `PRODUCT_FACT_NOT_VERIFIED` 차단이 그대로 걸립니다.
`test_auto_post_gate_server_cases.py`·`test_auto_post_policy_v7.py`가 §15에 포함되어 통과했습니다.

실제 Naver 등록은 하지 않았습니다.

---

## 21. 전체 테스트

```
3526 passed in 1174.96s (0:19:34)
```

**3,526 passed / 0 failed / 0 skipped.**

| 시점 | passed | failed | 비고 |
|---|---|---|---|
| Phase 11-B 종료 | 3,526 | 0 | UTC 08-28, DPS 날짜 문제 잠복 |
| Phase 11-C (구버전 DB) | 3,522 | 4 | UTC 08-29, DPS 날짜 4건 발화 |
| Phase 11-C (신규 DB override) | 3,521 | 5 | 위 4건 + 리허설 부작용 1건 |
| **Phase 11-D (전환 후)** | **3,526** | **0** | DB 전환 + DPS 날짜 fixture 수정 |

**Product Facts 전환으로 새로 생긴 실패는 0건**이며,
기존 독립 결함이던 DPS 날짜 4건도 §6의 fixture 수정으로 해소되어 전체 0 failed를 달성했습니다.

부수적 검증: 이 전체 실행 도중 날짜가 2026-08-30으로 넘어갔습니다.
`UPCOMING_DATE`가 실행 시점 기준 상대 날짜이므로 날짜 경계를 넘고도 통과했으며,
이는 wall-clock 의존이 실제로 제거되었음을 보여줍니다.

---

## 22. Latency

새 기본 DB, warm-up 후 90회 측정.

| 대상 | 평균 | 중앙 | 최대 |
|---|---|---|---|
| 구버전 DB (Phase 11-B/11-C) | 2.66 ms | 3.22 ms | 4.29 ms |
| **신규 DB (기본 경로)** | 2.97 ms | **3.42 ms** | 4.46 ms |

파일이 2.36배(60 MB → 142 MB) 커졌지만 중앙값 차이는 **+0.2 ms**입니다.
인덱스가 동일하고 조회가 exact key 기반이라 크기 증가가 조회 비용으로 이어지지 않습니다.
GPT 호출이 수 초 단위인 것을 감안하면 **회귀라 할 수준이 아닙니다**.

---

## 23. Rollback

| 항목 | 값 |
|---|---|
| 백업 파일 | `data/product_facts_before_phase11d_20260829T143917Z.db` |
| 존재 | ○ (60,170,240 bytes) |
| SHA-256 | `cddf3082df82d87065a452ee8140af9f42c4d0b31e91753597717f55cc82ac4c` (구버전과 일치) |

복원 절차 (안전 결함 발생 시에만):

```
cp -f data/product_facts_before_phase11d_20260829T143917Z.db data/product_facts.db
# 이후 SHA-256이 cddf3082… 인지 확인
```

또는 파일을 건드리지 않고 `OJE_PRODUCT_FACTS_DB_PATH`를 백업 파일로 지정하는 방법도 있습니다
(단 `.env` 수정은 이번 Phase 금지 사항이므로 실행하지 않았습니다).

**이번 Phase에서 rollback은 수행하지 않았습니다.** 안전 결함이 없었기 때문입니다.
최종 상태는 새 Final DB를 사용하는 상태입니다.

---

## 24. Git 추적 상태 — 확인 필요한 위험

| 파일 | 추적 | 근거 |
|---|---|---|
| `data/product_facts.db` | **추적 중 (M)** | `.gitignore` 36행 `!data/product_facts.db` 화이트리스트 예외 |
| `data/product_facts_final.db` | 무시됨 | `.gitignore` 23행 `*.db` |
| `data/product_facts_before_phase11d_…db` | 무시됨 | `.gitignore` 23행 `*.db` |

`git status`에 `M data/product_facts.db`로 나타납니다.

### 위험: GitHub 파일 크기 한도 초과

새 DB는 **142,131,200 bytes = 135.5 MiB**입니다.
GitHub는 개별 파일 **100 MiB를 초과하면 push를 거부**합니다(경고가 아니라 하드 한도).

즉 현재 상태 그대로 commit 후 push하면 **거부될 가능성이 매우 높습니다.**
구버전(60,170,240 bytes = 57.4 MiB)은 한도 아래였으므로 지금까지 문제가 없었습니다.

§30에 따라 이번 Phase에서 `.gitignore` 변경, `git rm --cached`, Git LFS 도입을
**하지 않았습니다.** 상태와 위험만 보고합니다. 선택지는 다음 단계에서 사용자가 결정합니다.

---

## 25. 변경 파일

| 파일 | 변경 | 사유 |
|---|---|---|
| `data/product_facts.db` | **교체** (60 MB → 142 MB, `cddf3082…` → `e0cdd363…`) | 이번 Phase의 목적 |
| `tests/test_product_facts_safety_gate_11b.py` | tripwire 테스트 교체 | §4 |
| `tests/test_product_facts_b5.py` | absent-field 테스트 교체 | §5 |
| `tests/test_atomic_answer_completeness.py` | DPS 날짜 fixture 상대화 | §6 |
| `tests/test_atomic_draft_composition.py` | 동일 | §6 |
| `tests/test_delivery_pipeline_e2e_dps.py` | 동일 | §6 |
| `tests/test_golden_auto_post_core_e2e.py` | 동일 | §6 |
| `docs/phase11d_product_facts_activation.md` | 신규 | 이 보고서 |
| `data/product_facts_before_phase11d_20260829T143917Z.db` | 신규 (untracked) | 백업 |

**production 코드 변경 0건.** Phase 11-A/B/C의 기존 변경은 하나도 되돌리지 않았습니다.

---

## 26. 잔여 위험

**HIGH — 추적 중인 `data/product_facts.db`가 GitHub 파일 한도 초과 (§24)**
135.5 MiB > 100 MiB. 현 상태로 commit·push하면 GitHub가 거부합니다.
다음 단계에서 반드시 결정해야 할 사항입니다.

**MEDIUM — `set_top_box_*` 필드와 R2 gate의 개념 불일치**
새 DB에는 셋톱박스 고유 사실이 32개 상품에 있으나, `component_scope` 판정이
`accessory_` 접두사만 인식하므로 이들을 본체 사실로 분류합니다.
현재 `FIELD_TOPICS`에 매핑돼 있지 않아 무해하지만, 매핑 전에 설계가 필요합니다.

**MEDIUM — CONFLICT / NEEDS_REVIEW 증가**
CONFLICT 30 → 136, NEEDS_REVIEW(resolution) 318 → 717.
출처가 3배로 늘며 교차검증이 강화된 결과이고 안전계약이 자동 차단하므로 오답 위험은 없으나,
그만큼 답변하지 못하는 항목이 존재합니다. 상품DB 쪽 품질 정리 대상입니다.

**LOW — `CUSTOMER_INQUIRY` 1,110건의 `product_id` 부재**
Naver 고객문의 API 응답에 상품 식별자가 없어 Product Facts 조회가 불가능합니다. 구조적 한계입니다.

**LOW — 매핑되지 않은 VERIFIED 필드 140종**
대부분 식별자·이미지 메타·마케팅 문구·배송정책이지만,
`power_cable_included`(38) 등 일부는 답변 가치가 있습니다. 확장은 별도 판단 사항입니다.

---

## 27. 최종 상태

| 파일 | 역할 | SHA-256 | 크기 |
|---|---|---|---|
| `data/product_facts.db` | **실사용 Final DB** | `e0cdd363…6f55a078` | 142,131,200 |
| `data/product_facts_final.db` | 동일 artifact 검증용 copy (보존) | `e0cdd363…6f55a078` | 142,131,200 |
| `data/product_facts_before_phase11d_20260829T143917Z.db` | 구버전 rollback용 (보존) | `cddf3082…ac82ac4c` | 60,170,240 |

§29에 따라 `product_facts_final.db`와 백업을 **삭제하지 않았습니다.**
WAL/journal 잔여 파일도 없습니다.

`git status`: HEAD `3950e8b` 불변, commit/push 없음.
텍스트 변경은 8개 파일 +339/−28이며, 그중 production 코드는 Phase 11-B에서 온 3개 파일뿐입니다
(이번 Phase에서 production 코드를 추가로 수정하지 않았습니다).

---

## 28. 다음 단계

1. **Git 추적 정책 결정 (최우선)** — 135.5 MiB 바이너리를 어떻게 다룰지.
   선택지: `.gitignore` 화이트리스트 예외 제거 + `git rm --cached`, Git LFS 도입,
   또는 DB를 저장소 밖 artifact로 분리. 이번 Phase에서는 손대지 않았습니다.
2. **파일 정리** — `product_facts_final.db` 및 구버전 백업의 보존 기간 결정.
3. **서버 PC 배포** — 동일 artifact(`e0cdd363…`)를 배포하고 SHA-256으로 확인.
4. 그 다음에 `set_top_box_*` 구성품 scope 설계, 매핑 확장 검토.

---

## 29. 최종 판정

# PHASE 11-D READY — 최종 Product Facts DB 실사용 전환 완료

§34의 조건을 하나씩 대조합니다.

| 조건 | 결과 |
|---|---|
| Final DB SHA 일치 | ○ `e0cdd363…6f55a078`, final DB와 byte-identical |
| 기본 경로 전환 성공 | ○ runtime fingerprint로 확인 (override 없음) |
| SQLite integrity 정상 | ○ `ok`, 94 listings / 93 SUCCESS / 1 FAILED |
| R1 정상 | ○ 판매종료 상품 SEMI_STATIC 9건 차단, DYNAMIC 8건 차단, STATIC 18건 유지 |
| R2 정상 | ○ 구성품 질문 3건 차단, 단독 listing 2건 허용, 상속 0건 |
| R4 정상 | ○ brand/manufacturer 분리, 제품라인 근거 요건, 부정 추론 없음 |
| Product Facts 관련 회귀 0 | ○ 관련 13개 파일 539 passed |
| **WRONG 0** | ○ 3,196건 중 0 |
| **UNSUPPORTED_BUT_ANSWERED 0** | ○ 3,196건 중 0 |
| Missing Item 정책 유지 | ○ 4개 문구 전부 차단, 관련 92 passed |
| DPS routing 유지 | ○ `answer_service.py` 미변경, 관련 164 passed |
| Learning safety 유지 | ○ `learning_evidence_policy.py` 미변경 |
| Auto-post safety 유지 | ○ unsafe fact가 `excluded_facts`로 분류되어 gate 통과 불가 |
| rollback 가능 | ○ 백업 존재 + SHA 확인 + 절차 문서화 |

추가로 **전체 테스트 3,526 passed / 0 failed**를 달성했습니다.

§36에 따라 구버전 archive와 `product_facts_final.db`를 삭제하지 않았고,
서버 배포·Naver 등록·DPS 실행·git commit/push는 하지 않았습니다.
