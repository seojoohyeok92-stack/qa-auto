# Q&A auto 통합 6A: GPT Provider Governance와 Canary 준비

## 목적

기존 Rule/DPS/Facts/Fake GPT/Validator/승인 동작을 유지하면서 실제 Provider를
회사 승인 이후 안전하게 연결할 운영 통제 계층을 추가한다. 이 단계는 실제
OpenAI 호출을 활성화하지 않는다.

## 실행 모드

`QNA_GPT_MODE`의 기본값은 `FAKE`다.

| 모드 | Program Answer | Provider 동작 |
|---|---|---|
| `FAKE` | Fake GPT Validator 통과 결과, 실패 시 Rule | 네트워크 없음 |
| `SHADOW` | 항상 Rule Answer | GPT 결과는 비교 지표로만 저장 |
| `CANARY` | 선정 건만 GPT 후보, 직원 검토 필수 | 결정적 일부 문의 |
| `ACTIVE` | GPT Validator 통과 후보 | 직원 승인 필수 |
| `DISABLED` | Rule Answer | GPT 계층 건너뜀 |

Shadow 결과는 `answer_drafts.original_answer`를 덮어쓰지 않으며 승인/등록에
사용할 수 없다. Canary 선정 건은 Validator 통과 후에도
`NEEDS_REVIEW`와 `auto_answerable=false`다.

## Provider 설정

`GptProviderSettings`는 provider/model/mode, connect/read/total timeout,
retry/backoff, 분당·일일·문의별 한도, 재생성 cooldown, 일일 원화 비용 한도,
Canary 비율, 승인 상태와 모든 정책 버전을 관리한다.

기본 timeout:

```text
connect: 5초
read: 30초
total: 40초
```

API key 값은 설정 객체, UI, DB, metadata, 로그에 저장하지 않는다. 설정에는
존재 여부 boolean만 남긴다.

## 회사 승인 Gate

실제 Provider는 다음 조건을 모두 만족해야 초기화된다.

- 모드가 `SHADOW`, `CANARY`, `ACTIVE` 중 하나
- `QNA_GPT_COMPANY_APPROVED=true`
- 실제 provider 이름 설정
- 모델 설정
- `QNA_GPT_API_KEY` 존재
- 개인정보 보호 활성화
- 전체 설정 validation 통과

하나라도 빠지면 `GPT_CONFIGURATION_INVALID`를 기록하고 Rule fallback한다.
설정 화면에는 상태만 표시하며 API key 입력·저장·활성화 버튼은 없다.

`OpenAIJsonProvider`는 승인된 transport를 주입할 수 있는 경계만 제공한다.
OpenAI SDK import와 실제 네트워크 transport는 이번 단계에서 연결하지 않았다.

## 개인정보 보호

`PromptPrivacyService`는 외부 Provider 호출 전에 payload를 재검사한다.

제거 필드:

- 주문번호, 상품주문번호, 문의/고객 ID
- 고객명·표시명·전화·이메일·주소
- DPS 판매번호
- OTP, password, API key, token
- authorization, Cookie, Session

마스킹 pattern:

- 전화번호, 이메일, 상세 주소, 긴 주문번호
- 인증정보 할당문
- 내부 URL
- Windows 내부 파일 경로

인증정보, 내부 URL/경로가 발견되면 `safe_to_send=false`이며 실제 Provider
호출을 차단한다. Fake 모드는 네트워크가 없지만 같은 audit 결과를 metadata에
남긴다. 원문 개인정보는 metadata에 저장하지 않는다.

## Prompt 감사 정책

기본 저장 정보:

- provider/model/mode
- prompt/privacy/validator/tone/governance 버전
- correlation ID
- 시작·완료 시각과 duration
- 성공/오류 유형과 마스킹된 메시지
- 입력/출력 문자 수
- 제공된 경우 token usage
- 계산 가능한 경우 원화 비용
- Privacy 제거 개수
- Validator/fallback/Canary/Shadow 지표

DB 스키마에는 Prompt 전문과 GPT 응답 전문 컬럼이 없다. 원문 capture 설정은
회사 승인과 보안 승인 플래그 두 개가 동시에 있어야 true가 되지만, 이번
단계에는 실제 원문 저장 구현 자체가 없다.

## Migration v5

`gpt_provider_runs`를 추가했다.

```text
id, inquiry_id, draft_id, correlation_id
provider, model, mode
prompt_version, policy_version
privacy_policy_version, validator_policy_version, company_tone_version
started_at, completed_at, duration_ms
success, error_type, error_message_masked
input_size, output_size
input_tokens, output_tokens, total_tokens
estimated_cost_krw
privacy_removed_count, validator_passed, fallback_used
retry_count, canary_selected
shadow_comparison_json, created_at
```

문의와 초안이 삭제되더라도 운영 감사 이력은 `SET NULL`로 보존한다.
migration 1~4는 수정하지 않았다.

## Timeout과 Retry

`ResilientJsonProvider`가 provider adapter 밖에서 retry를 담당한다.

재시도:

- connection/read timeout
- 일시적 연결 오류
- HTTP 429 성격 오류
- 일부 5xx 성격 오류

재시도 금지:

- 인증/권한 오류
- 400 성격 오류
- JSON 계약 위반
- Privacy 차단
- Validator 실패

backoff는 `base * 2^(attempt-1)`이며 sleeper와 clock을 주입할 수 있어
테스트에서 실제 대기하지 않는다. 전체 timeout을 넘길 retry는 시작하지
않는다. timeout은 `GPT_PROVIDER_TIMEOUT`으로 구분하고 Rule fallback한다.

## Rate Limit

- 분당 요청 제한
- 일일 요청 제한
- 문의별 일일 호출 제한
- 동일 문의 재생성 cooldown

초과 시 Provider를 호출하지 않고 `GPT_PROVIDER_RATE_LIMITED`와
`GPT_RULE_FALLBACK`을 기록한다. 앱 전체 오류로 승격하지 않는다.

## 비용 관리

`answer/gpt_pricing.py`가 모델별 원화 단가를 한곳에서 관리한다. 현재
`fake-json-v1`만 0원으로 정의했다. 실제 모델 단가는 관리자가 승인된 원화
단가로 추가해야 한다.

Provider가 token usage를 주면 누적해 비용을 계산한다. 단가나 usage가 없으면
DB에 `NULL`(UNKNOWN)을 저장하며 임의 비용을 생성하지 않는다. 일일 비용
한도 초과 시 `GPT_PROVIDER_COST_LIMITED` 후 Rule fallback한다. 환율 자동
조회는 구현하지 않았다.

## Canary

SHA-256의 inquiry ID 기반 bucket으로 결정한다. 같은 문의는 같은 비율
설정에서 항상 같은 그룹이다.

제외:

- Privacy 제거/차단 가능성이 있는 문의
- Rule 강제 검토
- 환불·반품·법적·분쟁·보상 고위험 문의
- 허용되지 않은 문의 유형
- posted 문의

posted 문의는 AnswerService의 기존 보호 Gate에서 governance 진입 전에
차단된다.

## Shadow

GPT Draft/Self Review/Validator를 실행하지만 Program Answer는 Rule이다.
별도 비교 JSON에 다음만 저장한다.

- Validator 통과 여부
- Rule/GPT 길이
- 질문 개수
- used facts
- missing information

GPT 답변 전문은 저장하지 않는다.

## Characterization Fixture

`tests/fixtures/gpt_governance_characterization.json`에 개인정보가 아닌 12개
범주의 계약 fixture를 추가했다.

- 일반 상품, 배송, 설치, 복합
- 불만, 감사, 짧은 질문, 오타
- 개인정보 포함, 사실 부족
- DPS 실패
- 환불·반품 고위험

각 fixture는 허용/금지 요소, Facts, required used facts,
requires_review와 Validator 기대값을 가진다. 기본 pytest는 Fake/stub만
사용한다.

## 정책 버전

초안 metadata와 provider run에 다음 버전을 고정 저장한다.

- `prompt_version`
- `privacy_policy_version`
- `validator_policy_version`
- `company_tone_version`
- `policy_version`

기존 초안은 변경하지 않으며 새 생성 이력에 당시 버전이 기록된다.

## AnswerService 통합

기존 `HybridAnswerService`를 변경하지 않고
`GovernedHybridAnswerService`가 바깥에서 감싼다.

```text
Rule → Facts → Privacy → Mode/Quota → Hybrid → Validator → Program Answer
```

기본 FAKE 동작은 5단계와 같다. 생성 완료 후 run의 `draft_id`만 연결한다.
Provider 실패가 Rule, DPS, 직원 수정, Final, 승인, posted 데이터를 변경하지
않는다.

## UI

기존 Dashboard 디자인은 유지했다.

설정 메뉴:

- Mode/Provider/Model
- 회사 승인/API key 존재 여부
- 오늘 요청/실패/fallback/Privacy 차단
- 평균 응답시간
- 오늘 비용과 한도
- Canary 비율과 Rate 상태

모두 읽기 전용이다.

문의 GPT 진단:

- Mode/Provider/Model/소요시간
- Prompt 버전
- retry 횟수
- Validator와 Privacy 상태
- fallback 사유
- Shadow/Canary 여부

## Activity Log

```text
GPT_PROVIDER_REQUESTED
GPT_PROVIDER_SUCCEEDED
GPT_PROVIDER_FAILED
GPT_PROVIDER_TIMEOUT
GPT_PROVIDER_RATE_LIMITED
GPT_PROVIDER_COST_LIMITED
GPT_PRIVACY_BLOCKED
GPT_SHADOW_COMPLETED
GPT_CANARY_SELECTED
GPT_CANARY_SKIPPED
GPT_RULE_FALLBACK
GPT_CONFIGURATION_INVALID
```

Prompt/응답 전문은 기록하지 않고 오류 문자열은 중앙 마스킹을 적용한다.

## 테스트와 Windows 검증

2026-07-29 기준 실제 검증 결과:

- 변경 전 기준선: `416 passed`
- 6A 신규 테스트: `122 collected`, 전체 실행에 포함되어 모두 통과
- 변경 후 전체: `538 passed`
- 소스 컴파일: `app.py`, `config.py`, `answer`, `api`, `dps`,
  `repositories`, `services`, `ui`, `tests` 모두 통과
- Streamlit health: `http://127.0.0.1:8770/_stcore/health`가
  `HTTP 200`, 본문 `ok` 반환
- Streamlit AppTest: 전체 앱, 설정 진단, 문의별 GPT 진단 모두 예외 0건

Windows fixture 검증:

- API key 없음 + FAKE: Fake 결과와 Validator 정상, API key 오류 없음
- 회사 승인 없음 + ACTIVE: 실제 호출 차단, Rule fallback, 앱 유지
- SHADOW: Program Answer는 Rule 유지, 비교 metadata와 run만 저장
- CANARY 100% fixture: 결정적 선정, Validator 통과, 직원 검토 필수,
  posted 아님
- Provider timeout: Rule fallback, timeout event 기록, 기존 DPS metadata와
  기존 draft 보존

실제 외부 Provider 네트워크 호출은 회사 승인과 API key가 없으므로 수행하지
않았다.

## 환경변수 예시

실제 값은 넣지 않는다.

```text
QNA_GPT_MODE=FAKE
QNA_GPT_PROVIDER=fake
QNA_GPT_MODEL=fake-json-v1
QNA_GPT_COMPANY_APPROVED=false
QNA_GPT_PRIVACY_ENABLED=true
QNA_GPT_CONNECT_TIMEOUT_SECONDS=5
QNA_GPT_READ_TIMEOUT_SECONDS=30
QNA_GPT_TOTAL_TIMEOUT_SECONDS=40
QNA_GPT_MAX_RETRIES=2
QNA_GPT_REQUESTS_PER_MINUTE=30
QNA_GPT_DAILY_REQUEST_LIMIT=1000
QNA_GPT_PER_INQUIRY_LIMIT=5
QNA_GPT_DAILY_COST_LIMIT_KRW=0
QNA_GPT_CANARY_PERCENTAGE=0
QNA_GPT_PROMPT_VERSION=prompt-v1
QNA_GPT_PRIVACY_POLICY_VERSION=privacy-v1
QNA_GPT_VALIDATOR_POLICY_VERSION=validator-v1
QNA_GPT_COMPANY_TONE_VERSION=tone-v1
QNA_GPT_POLICY_VERSION=governance-v1
```

`QNA_GPT_API_KEY`는 문서나 UI에 실제 값을 넣지 않는다.

## 운영 전 체크리스트

1. 회사 승인과 보안/개인정보 검토
2. 실제 provider/model 명시
3. secret store를 통한 API key 공급
4. pricing 원화 단가 승인
5. Shadow characterization 비교
6. 낮은 Canary 비율과 직원 강제 검토
7. timeout/retry/rate/cost alert 확인
8. Validator 실패·Rule fallback 감사
9. 실제 등록 Gate와 승인 상태 분리 확인

## 기술부채와 다음 단계

- 실제 OpenAI transport/SDK는 연결되지 않았다.
- background job queue가 없어 Shadow 호출은 현재 답변 생성 요청 안에서
  수행된다.
- 다중 Streamlit 프로세스의 엄격한 분당 rate limit은 DB 기반 sliding
  window이며 분산 lock은 없다.
- 실제 Provider usage/cost 형식별 adapter가 필요하다.
- 직원 Shadow 선호 평가는 아직 저장하지 않는다.
- Prompt capture는 구현하지 않았으며 계속 비활성이다.

다음 단계는 승인된 Provider transport를 Shadow 전용으로 연결하고 실제
characterization 결과를 축적하는 것이다. 충분한 Shadow 검증 전에는 Canary
또는 ACTIVE를 운영하지 않는다.
