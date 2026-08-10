# Q&A auto 통합 5단계: GPT Hybrid Answer Engine

## 목적

Rule Engine을 제거하거나 대체하지 않고 Naver 문의 사실, 주문 사실, DPS
사실과 Rule 결과 위에 GPT 이해·초안·자체 검토·검증 계층을 추가한다.
이번 단계의 GPT provider는 네트워크를 사용하지 않는 `FakeGptProvider`뿐이다.
실제 OpenAI, Azure, Claude, Gemini 호출은 비활성 상태다.

## 전체 구조

```text
문의
  → Rule Engine
  → Naver 문의·주문 metadata
  → DPS enrichment
  → AnswerFacts
  → GPT Understanding
  → GPT Draft
  → GPT Self Review
  → deterministic Validator
  → Program Answer
  → 직원 수정
  → 승인
  → 등록(아직 비활성)
```

`AnswerService`는 기존처럼 문의를 읽고 DPS를 enrichment한 후
`AnswerEngine`에서 Rule Answer를 먼저 만든다. 이후
`HybridAnswerService`가 GPT 계층을 실행한다. 어느 GPT 단계에서든 예외나
검증 실패가 발생하면 GPT 답변을 폐기하고 원래 Rule Answer를 저장한다.

## Provider 구조

```text
answer/providers/
  base.py                 기존 Rule provider interface
  rule_provider.py        항상 유지되는 규칙 provider
  openai_provider.py      기존 비활성 placeholder
  interfaces.py           JsonGptProvider Protocol
  gpt_provider.py         교체 가능한 GPT 추상 기반
  fake_gpt_provider.py    네트워크 없는 JSON provider
  provider_factory.py     provider 선택과 승인 차단
```

`QNA_GPT_PROVIDER` 기본값은 `fake`다. `openai`, `azure`, `claude`,
`gemini` 선택은 회사 승인 전까지 명시적인
`AnswerProviderUnavailableError`를 발생시킨다. API key가 없어도 앱과
테스트가 정상 동작하고 import만으로 네트워크 요청이 발생하지 않는다.

## AnswerFacts

`answer/facts.py`의 `AnswerFacts`는 다음 섹션을 갖는다.

- inquiry: 정규화된 질문, 유형, 등록 시각
- product: 상품명, 옵션명
- order: 주문·결제·배송기한 정보
- delivery: 확인된 배송 상태
- installation: 설치 상태, 예정일, 시간 문구, 유형
- dps: 조회 상태, 오류, 캐시 및 정책 결과
- rule: Rule 상태, 답변, 분류, 근거, 검토 정책
- activity: 전달이 허용된 최소 활동 정보
- policy: facts-only, 추측 금지, 검토 필요 정책
- warnings: Rule/DPS 경고

업무 DB에는 order ID를 유지하지만 GPT prompt를 만들 때 inquiry ID,
question ID, order ID, product order ID, DPS 판매번호를 제거한다. 고객 표시
정보는 AnswerFacts에 넣지 않는다.

## GPT Understanding

`services/gpt_understanding_service.py`는 JSON을 `IntentResult`로 검증한다.

```text
category
questions[]
emotion = NORMAL | CONFUSED | URGENT | ANGRY | THANKFUL | FOLLOW_UP
urgency
confidence (0..1)
requires_review
reason
```

복합 질문은 줄바꿈과 물음표/느낌표 단위로 분리해 metadata에 보존한다.
Fake provider는 정해진 keyword만으로 결정적으로 감정과 긴급도를 분류한다.

## GPT Draft

`services/draft_generation_service.py`는 반드시 다음 JSON 계약을 사용한다.

```json
{
  "answer": "...",
  "confidence": 0.97,
  "used_facts": ["rule.answer"],
  "missing_information": [],
  "requires_review": false,
  "warnings": []
}
```

Fake provider의 기본 초안은 검증된 `rule.answer`를 사용한다. DPS가 반영된
Rule Answer 역시 동일하게 Facts가 된다. 자유 문장만 반환하거나 list/숫자
형식이 잘못된 JSON은 parsing 단계에서 거부된다.

## Prompt Builder

`answer/prompt_builder.py`는 회사 말투, 답변 규칙, 금지사항, 정제된 Facts,
JSON 출력 계약만 포함한다. SQL, DPS 화면/컨트롤, UIA, 내부 코드 구조는
포함하지 않는다.

Prompt 구성 전 다음을 제거하거나 마스킹한다.

- 전화번호와 이메일
- 주소 형태
- OTP, password, secret
- API key와 access/refresh token
- authorization, Cookie, Session
- 고객 표시 정보와 업무 식별자

## GPT Self Review

`services/self_review_service.py`는 다음을 JSON으로 다시 검사한다.

- 모든 하위 질문에 답했는지
- 추측 표현이 있는지
- Facts와 일치하는지
- 직원 검토가 필요한지

Self Review가 실패하거나 사실 불일치를 보고하면 Validator가 GPT 답변을
승인하지 않는다.

## Validator

`answer/answer_validator.py`는 GPT와 독립된 결정적 코드다.

- `used_facts` 경로가 실제 AnswerFacts에 존재하는지
- ISO 날짜가 확인된 설치일/배송기한과 일치하는지
- 개인정보·인증정보 형태가 포함되지 않았는지
- 추측 문장이 있는지
- Rule의 직원 검토 정책을 GPT가 해제하지 않았는지
- 배송/설치 사실 없이 완료·반품·기사 방문을 확정하지 않는지
- Self Review가 통과했는지

실패하면 `GPT_VALIDATION_FAILED`, `GPT_FALLBACK_RULE`을 기록하고 Rule
Answer를 Program Answer로 사용한다.

## Fallback

다음 경우 모두 Rule fallback이다.

- provider 예외
- JSON parsing 실패
- 빈 GPT 답변
- 존재하지 않는 Fact 사용
- Facts에 없는 날짜
- 추측·허위 확정 문장
- 개인정보·인증정보 노출
- 자체 검토 실패
- Rule 검토 정책 위반

Fallback metadata에는 사유와 가능한 Fact 목록, 완료된 GPT 중간 결과를
저장한다. RuleProvider와 기존 AnswerEngine은 삭제하거나 변경하지 않았다.

## DB migration v4

기존 migration 1~3은 수정하지 않았다.

```text
answer_drafts.metadata_json TEXT NOT NULL DEFAULT '{}'
idx_answer_drafts_provider_created
```

GPT intent, confidence, used/missing facts, self review, validator 결과와 fallback
상태가 초안 이력마다 저장된다. 답변 본문, 직원 수정본, Final Answer,
posted 보호 정책은 기존 구조를 그대로 사용한다.

## UI

기존 Dashboard와 3단 검토 작업공간을 유지한다. Program Answer 아래에
다음을 추가했다.

- GPT Confidence
- Emotion
- Intent
- Validator PASS/FAIL/RULE FALLBACK
- 분리된 Questions
- Used Facts
- Missing Facts
- Warnings
- Validator errors와 checked facts

직원 수정, 자동 저장, 승인, 승인 취소, posted 잠금은 4단계 정책을 그대로
따른다.

## Activity Log

다음 이벤트를 문의별로 기록한다.

```text
GPT_ANALYSIS_STARTED
GPT_ANALYSIS_COMPLETED
GPT_DRAFT_CREATED
GPT_SELF_REVIEW
GPT_VALIDATION_FAILED
GPT_FALLBACK_RULE
GPT_APPROVED
```

Prompt와 답변 전문은 로그에 기록하지 않는다. provider, 분류, confidence,
질문 개수, used fact 이름, validation 오류 코드 등 최소 metadata만 기록하고
기존 LogRepository의 민감정보 마스킹을 거친다.

## 테스트

```powershell
Set-Location -LiteralPath "C:\path\to\qa-auto"
python -m pytest -ra --basetemp ".pytest_tmp_manual"
python -m compileall .
```

신규 테스트는 Fake GPT, provider factory, Facts, 질문 분리, 감정, JSON
parsing, Draft, Prompt 마스킹, Validator, Self Review, Rule fallback,
metadata 저장, UI 진단 presenter, workflow, activity log를 검증한다.

## 알려진 제한과 기술부채

- Fake provider는 실제 LLM의 언어적 재구성 품질을 평가하지 않는다.
- 복합 질문별 개별 Rule 재평가는 현재 DPS/Rule 결과의 기존 결합 구조를
  이용하며 완전한 문장 의미 분할기는 아니다.
- Validator의 허위정보 탐지는 결정적 pattern과 Fact path 검사 중심이다.
- 정책 문서의 버전 관리와 승인 workflow는 아직 별도 테이블이 아니다.
- Naver 주문 facts는 현재 source adapter가 보유한 필드로 제한된다.

## 아직 구현하지 않은 기능

- 실제 OpenAI/Azure/Claude/Gemini 호출
- 실제 네이버 답변 등록
- OTP/인증 구조 변경
- GPT를 이용한 학습 자동 반영
- 실제 provider 비용·rate limit·재시도 관리

## 다음 단계

회사 승인 후 실제 provider adapter를 별도 패키지로 구현하되
`JsonGptProvider.generate_json()` 계약을 유지한다. 실제 호출 전에는
Prompt 감사, provider 응답 원문 보관 정책, 비용/timeout/rate limit,
개인정보 영향평가와 모델별 characterization test를 먼저 추가해야 한다.
실제 등록 서비스는 Validator 통과 또는 직원 승인된 Final Answer만
받도록 별도 gate를 둔다.
