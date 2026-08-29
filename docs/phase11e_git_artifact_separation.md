# Phase 11-E — Product Facts DB Git 분리 및 Phase 11 배포

작업일: 2026-08-30 · 서버 PC 미접속 · Product Facts DB 내용 미변경

> **핵심을 먼저 적습니다.**
> Product Facts DB(135.5 MiB)를 Git 추적에서 분리했습니다. 파일은 개발 PC에 그대로 있고
> Git index에서만 제거했습니다(`git rm --cached`).
> 서버 PC의 `git pull` 영향은 **추측하지 않고 임시 Git fixture로 재현**했습니다.
> 결론: **서버의 DB는 pull 전에 반드시 별도 보호가 필요합니다.** 절차는 §17에 있습니다.

---

## 1. 시작 Git 상태

| 항목 | 값 |
|---|---|
| branch | `main` |
| HEAD | `3950e8b` |
| origin/main | `3950e8b` (동일 — Phase 11 작업은 아직 push 전) |

작업 트리 변경 분류:

| 분류 | 파일 |
|---|---|
| A. Phase 11-A 보고서 | `docs/phase11a_product_facts_integration_audit.md` |
| B. Phase 11-B production safety gate | `repositories/product_fact_repository.py`, `services/product_knowledge_service.py`, `scripts/export_inquiry_diagnostics.py` |
| C. Phase 11-B tests | `tests/test_product_facts_safety_gate_11b.py` |
| D. Phase 11-C 보고서 | `docs/phase11c_product_facts_artifact_deployment.md`, `docs/phase11c_final_db_compatibility.md` |
| E. Phase 11-D tests/보고서 | `tests/test_product_facts_b5.py`, `tests/test_atomic_answer_completeness.py`, `tests/test_atomic_draft_composition.py`, `tests/test_delivery_pipeline_e2e_dps.py`, `tests/test_golden_auto_post_core_e2e.py`, `docs/phase11d_product_facts_activation.md` |
| F. DB 파일 변경 | `data/product_facts.db` (구버전 → 최종 artifact) |
| G. 기존 사용자 변경 | **없음** |
| H. 무관한 변경 | **없음** |

G·H가 비어 있으므로, commit에 Phase 11과 무관한 변경이 섞일 위험이 없습니다.

---

## 2. DB Hash

| 파일 | size | mtime | SHA-256 |
|---|---|---|---|
| `data/product_facts.db` | 142,131,200 | 2026-08-29 23:42:01 | `e0cdd3639cb4f0c5f9bc3f2d1f3c4caf020deca45b9144590614e4bc6f55a078` |
| `data/product_facts_final.db` | 142,131,200 | 2026-08-29 02:20:20 | 동일 |
| `data/product_facts_before_phase11d_20260829T143917Z.db` | 60,170,240 | 2026-08-25 12:30:32 | `cddf3082df82d87065a452ee8140af9f42c4d0b31e91753597717f55cc82ac4c` |

앞의 두 파일이 §5의 기대 SHA와 일치하여 Git 작업을 진행했습니다.

---

## 3. 기존 Git 추적 상태

| 파일 | 작업 전 | 근거 |
|---|---|---|
| `data/product_facts.db` | **TRACKED** | `.gitignore` 36행 `!data/product_facts.db` 화이트리스트 예외 |
| `data/product_facts_final.db` | IGNORED | `.gitignore` 23행 `*.db` |
| `data/product_facts_before_phase11d_…db` | IGNORED | `.gitignore` 23행 `*.db` |
| `data/archive/*.db` | 해당 없음 | `data/archive/` 디렉터리가 존재하지 않음 |

`git ls-files data/` 결과는 `data/product_facts.db` **한 건뿐**이었습니다.
즉 저장소가 추적하던 DB는 이 파일 하나입니다.

---

## 4. Git History

| 항목 | 값 |
|---|---|
| 추적 시작 커밋 | `4696d20 feat: finalize Q&A auto product facts and safety gates` |
| 이후 변경 이력 | 없음 (그 커밋이 유일) |
| HEAD에 존재 | 예 — **60,170,240 bytes (구버전)** |
| origin/main에 존재 | 예 — 동일 |

**history는 삭제·rewrite하지 않았습니다.** 과거 커밋에 담긴 60 MB blob은 그대로 남습니다.
목표는 "앞으로 Product Facts DB를 코드 Git에서 관리하지 않는다"이며, 과거 정리는 범위 밖입니다.

---

## 5. Git 정책

| 대상 | 배포 경로 |
|---|---|
| production code / tests / docs / config·schema 텍스트 | **GitHub** |
| `data/product_facts.db` | **수동 artifact** (Git 배포 대상 아님) |

```
상품DB PC   Master 생성·품질검증 → SHA-256 공표
개발 PC     Master의 byte-identical copy
서버 PC     동일 artifact 수동 배포, 같은 SHA-256으로 확인
```

전환 이유: 최종 artifact가 57 MiB → **135.5 MiB**로 커져
GitHub의 개별 파일 **100 MiB 하드 한도**를 초과했습니다. 저장소가 더 이상 담을 수 없습니다.

---

## 6. .gitignore

`*.db`(23행)가 이미 모든 DB를 무시하고 있었고, 36행의 `!data/product_facts.db` 예외 하나만
그 파일을 되살리고 있었습니다. 따라서 **예외를 제거하는 것만으로 충분**하며,
`data/` 전체나 `*.db` 같은 새 광역 규칙을 추가하지 않았습니다.

- 제거: `!data/product_facts.db` 화이트리스트 예외
- 대체: 새 정책(수동 artifact, 3-PC 흐름, SHA-256 확인)을 설명하는 주석
- **다른 DB의 기존 정책은 한 줄도 바꾸지 않았습니다** — 운영 `oje_automation.db`,
  WAL/SHM 사이드카, `data/backups/` 등은 그대로 제외 상태를 유지합니다.

적용 후 세 파일 모두 `IGNORED (.gitignore:23:*.db)`로 분류됩니다.

---

## 7. 서버 Pull 위험 실험

**추측하지 않고 임시 Git fixture로 재현했습니다.** 실제 서버 PC는 건드리지 않았습니다.

구성: bare `origin.git` + `dev` 클론 + `server` 클론.
커밋 A가 `data/product_facts.db`를 추적하는 상태에서,
커밋 B로 `.gitignore` 예외 제거 + `git rm --cached`를 푸시한 뒤 `server`에서 `git pull`.

### 시나리오 1 — 서버 DB가 로컬 수정된 상태 (**실제 서버 상태**)

서버는 추적 중이던 구버전 파일 위에 최종 DB를 수동 복사했으므로,
Git이 보기에 그 파일은 **locally modified**입니다.

```
error: Your local changes to the following files would be overwritten by merge:
        data/product_facts.db
Please commit your changes or stash them before you merge.
Aborting
```

| 결과 | 값 |
|---|---|
| DB 파일 | **보존됨** (내용 그대로) |
| HEAD | **갱신 안 됨** (커밋 A에 머무름) |

→ **데이터 손실은 없지만 pull이 실패**하여 코드 업데이트를 받지 못합니다.

### 시나리오 2 — 서버 DB가 HEAD와 동일한 상태

```
Fast-forward
 .gitignore            | 1 -
 data/product_facts.db | 1 -
 delete mode 100644 data/product_facts.db
```

| 결과 | 값 |
|---|---|
| DB 파일 | **★삭제됨★** (경고 없이) |
| HEAD | 갱신됨 |

→ 파일이 수정되지 않은 상태였다면 **조용히 삭제**됩니다.

### 시나리오 3 — 안전 절차 검증

1. DB를 저장소 **밖으로** 백업
2. `git checkout -- data/product_facts.db` (로컬 수정 되돌려 pull 차단 해제)
3. `git pull` — Git이 파일을 삭제
4. 복원

여기서 추가로 발견한 점: **3단계에서 Git이 `data/` 디렉터리째 삭제**할 수 있습니다
(그 디렉터리에 추적/미추적 파일이 하나도 남지 않는 경우). 첫 시도의 복원이
`No such file or directory`로 실패해 드러났습니다.
실제 서버의 `data/`에는 `oje_automation.db` 등 다른 파일이 있어 디렉터리가 남을 가능성이 높지만,
절차에 `mkdir -p data`를 넣어 두면 어느 쪽이든 안전합니다.

보완한 절차로 재검증한 최종 상태:

```
HEAD        : 커밋 B로 갱신됨
DB 파일     : 최종 내용 그대로 보존
추적 여부   : 0건 (untracked)
ignore 확인 : .gitignore:*.db  data/product_facts.db
git status  : 0건 (clean)
```

---

## 8. DB Git 제외 적용

```
git rm --cached data/product_facts.db
```

| 항목 | 결과 |
|---|---|
| 디스크 파일 | **존재** (142,131,200 bytes) |
| SHA-256 | `e0cdd363…6f55a078` **불변** |
| Git 상태 | `D data/product_facts.db` (index에서 삭제 staged) |
| 재분류 | `IGNORED (.gitignore:23:*.db)` |

`data/product_facts_final.db`와 구버전 백업도 IGNORED이며, **어느 파일도 삭제하지 않았습니다.**

---

## 9. Phase 11 Diff 검토

production 변경은 Phase 11-B의 3개 파일뿐입니다(**+230 / −5**).

| 파일 | 내용 |
|---|---|
| `services/product_knowledge_service.py` (+168) | `collection_status` gate(R1), component subject gate(R2), brand/manufacturer 분리·제품라인 근거 요건(R4), `ProductKnowledgeResult`에 `collection_status`·`component_subject` 노출 |
| `repositories/product_fact_repository.py` (+39) | `identity()` — 경로/크기/mtime, 선택적 SHA-256 |
| `scripts/export_inquiry_diagnostics.py` (+28/−5) | 진단에 `knowledge_db` 지문 추가(파일명만, 디렉터리 제외) |

검사 결과 production 변경분에서 다음이 **발견되지 않았습니다**:
`print(` 디버그, `breakpoint`/`pdb`, `TODO`/`FIXME`/`HACK`, 임시 SHA 비교 코드,
개인 PC 경로, 상품DB PC 경로.

Phase 11-D에서 손댄 것은 테스트 fixture뿐이며 production DPS·Product Facts 로직은 미변경입니다.

---

## 10. 민감정보 검사

commit 대상 텍스트 172,365자를 검사했습니다.

| 항목 | 결과 |
|---|---|
| API key / secret / token | 0건 |
| password 값 | 0건 |
| OTP | 0건 |
| `.env` 값 | 0건 |
| 휴대전화번호 | 0건 (탐지 9건은 상품번호 `10194603339`·`10198648691`의 부분 문자열 오탐) |
| 이메일 | 0건 (탐지 4건은 `@pytest.mark.parametrize` 오탐) |
| **로컬 절대경로 / 사용자 계정명** | **5건 발견 → 제거** |

발견된 5건은 문서 2개에 있던 `C:\Users\user\…` 형태입니다.

| 파일 | 건수 |
|---|---|
| `docs/phase11a_product_facts_integration_audit.md` | 3 |
| `docs/phase11c_product_facts_artifact_deployment.md` | 2 |

계정명을 제거하고 `<홈>\…`로 치환했습니다. 어떤 경로를 조사했는지라는 정보는 남고,
Windows 계정명은 저장소에 들어가지 않습니다. 재검사 결과 잔여 0건입니다.

---

## 11. DPS 날짜 테스트

Phase 11-D에서 fixture만 수정했고, 이번 Phase에서 그 결과를 재확인했습니다.

**원인**: 네 테스트가 DPS 예정일을 `2026-08-28`로 하드코딩했고,
`is_schedule_stale()`이 문의의 `created_at`(UTC)과 비교하므로
UTC 날짜가 그 날을 넘긴 순간 과거 일정이 되어 `REVIEW_REQUIRED`로 떨어졌습니다.

**수정**: 각 파일에서 실행 시점 기준 상대 날짜를 사용합니다.

```python
UPCOMING_DATE = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
UPCOMING_DATE_KR = korean_date(UPCOMING_DATE)
```

§18이 경계한 "wall-clock에 따라 내일 다시 실패하는 `datetime.now()`식 임시방편"에 해당하지 않습니다.
값이 **매 실행 시점에 다시 계산**되어 항상 "문의 등록일 다음 날"이 되므로,
날짜가 아무리 흘러도 non-stale 조건이 유지됩니다.
실제로 Phase 11-D의 전체 테스트 실행 도중 날짜가 2026-08-30으로 넘어갔는데도 통과했습니다.

한국어 렌더링은 production의 `answer/answer_format.py::korean_date()`를 재사용해
"그 날짜가 답변에 도달했는가"라는 단언의 의미를 보존했습니다.
기대값(`SAFE`)은 낮추지 않았고, `is_schedule_stale()` 등 production 로직은 미변경입니다.

`2026-08-26`처럼 다른 의미(일정 변경 요청)를 가진 날짜는 그대로 두었습니다.

### stale 반대 계약 (§19)

`tests/test_stale_dps_and_payment_benefit_v11.py`가 이미 양방향을 검증하고 있으며
**32 passed**입니다. 이 파일은 명시적 `registered_at`을 쓰므로 wall-clock에 의존하지 않습니다.

| 입력 | 기대 |
|---|---|
| 일정 `2026-08-03`, 등록 `2026-08-22` | stale **True** |
| 일정 `2026-08-22`, 등록 `2026-08-22` | stale False (당일은 stale 아님) |
| 일정 `2026-08-25`, 등록 `2026-08-22` | stale False |
| `None` / `""` | stale False |

여기에 `test_stale_schedule_hard_blocks_auto_post`가
**과거 일정 → 자동등록 하드 차단**을 확인합니다.
즉 "과거 예정일은 확정 답변을 막는다"는 안전정책은 약화되지 않았습니다.

---

## 12. Product Facts DB 전환 관련 테스트 (§20)

Phase 11-D에서 바꾼 두 테스트를 다시 검토했습니다.

**tripwire** — `assert 모든 listing이 COLLECTION_SUCCESS`는 더 이상 정상계약이 아닙니다.
현재 최종 DB는 **93 SUCCESS / 1 FAILED**이며, 실제 계약은
"non-success listing의 unsafe fact가 차단된다"입니다.
새 테스트 `test_real_db_uncollected_listing_is_actually_gated`가 그것을 검증합니다 —
non-success listing을 순회하며 ① usable은 `STATIC_PRODUCT_FACT`만 남고
② VERIFIED `SEMI_STATIC_POLICY_FACT`는 전부 `COLLECTION_STATUS_NOT_CURRENT`로 차단됨을 단언합니다.

**absent-field** — 특정 상품의 우연한 결측을 하드코딩하지 않고
실행 시점에 실제 공백을 찾아 "공백이 부정 진술이 되지 않는다"를 검사합니다.

두 테스트 모두 **특정 DB의 우연한 상태를 계약으로 삼지 않습니다.**

---

## 13. Product Facts 관련 테스트

Product Facts / guard / Learning conflict / auto-post gate / pre-generation gate /
missing item / diagnostic export / stale DPS / DPS 날짜 4개 파일 — 총 18개 파일:

**735 passed / 0 failed**

---

## 14. 전체 테스트

(아래에서 갱신)

---

## 15. DB 무변경

(아래에서 갱신)

---

## 16. Staged 파일 / Commit / Push

(아래에서 갱신)

---

## 17. 서버 PC 다음 절차

(아래에서 갱신)

---

## 18. 잔여 위험 / 최종 판정

(아래에서 갱신)
