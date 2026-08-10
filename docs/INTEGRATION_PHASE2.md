# Q&A auto 통합 2단계

## 목표

자동 Q&A 원본의 검증된 규칙 기반 답변 생성 기능을 선별 이식했다.

```text
문의 선택 → 입력 정규화 → 답변 후보와 판단 사유 생성
→ 프로그램 원본 답변 저장 → 기존 문의 상세 화면에서 확인
```

네이버 실제 답변 등록은 아직 구현하거나 노출하지 않았다.

## 원본 분석과 이식 범위

파일별 역할, 의존성, 부수효과 및 이식 판단은
[AUTO_QNA_SOURCE_ANALYSIS.md](AUTO_QNA_SOURCE_ANALYSIS.md)에 기록했다.

선별 이식한 요소는 다음과 같다.

- `qna_auto/engine.py`: 분류와 답변 생성 규칙
- `qna_auto/answer_format.py`: 답변 형식과 고정 안내문
- `qna_auto/text_utils.py`: 비교·정규화 유틸리티
- `qna_auto_configs`의 정책, 배송, 이벤트, 모델 코드 JSON
- `학습자료/model_data_with_color.json`
- `configuration.xlsx`에서 추출한 활성 설치 일정 규칙 9건

복사한 JSON은 원본과 SHA-256이 같음을 확인했다. Excel 파일 자체는 런타임
생성·수정 부수효과와 `openpyxl` 의존성을 피하기 위해 복사하지 않았다.
원본의 활성 학습 규칙은 0건이었다.

다음은 이식하지 않았다.

- `naver_client.py`: 기존 네이버 인증·조회 구현과 중복
- `naver_workflow.py`: 실제 등록과 결과 파일 쓰기 포함
- `learning.py`: JSONL, Excel 및 설정 쓰기 포함
- `config_excel.py`: Excel 생성·수정 포함
- `openai_judge.py`: API 키와 외부 네트워크 의존
- `openai_config.json`, `.env`, `GPT API.env`, EXE, 로그, output, 인증 토큰,
  과거 실행 결과

## 새 answer 패키지

```text
answer/
├─ models.py
├─ engine.py
├─ answer_format.py
├─ text_utils.py
├─ config_loader.py
├─ source_adapter.py
├─ exceptions.py
└─ providers/
   ├─ base.py
   ├─ rule_provider.py
   └─ openai_provider.py
```

이 패키지는 Streamlit, SQLite, 네이버 API 및 DPS를 직접 참조하지 않는다.
기본 provider는 `RuleProvider`다. `OpenAIProvider`는 명시적으로
비활성화되어 있으며 import만으로 환경 파일을 읽거나 API 요청을 하지 않는다.

## 설정 및 학습 데이터

```text
answer_data/
├─ configs/
│  ├─ answer_policy.json
│  ├─ shipping_config.json
│  ├─ event_config.json
│  ├─ model_codes.json
│  └─ install_schedule_rules.json
└─ learning/
   └─ model_data_with_color.json
```

경로는 현재 실행 위치가 아니라 `Path(__file__)`에서 계산한다. 누락 또는
잘못된 JSON은 필요한 경로가 포함된 `AnswerConfigError`로 구분되며, 설정
캐시는 테스트에서 초기화할 수 있다.

## 입력과 출력

`AnswerRequest`는 `inquiry_id`, `question_id`, `store_code`, `inquiry_type`,
`question`, `product_name`, `option_name`, `customer_display`, `order_id`,
`product_order_id`, `existing_answer`, `metadata`를 갖는다. 누락 가능한 값은
안전하게 처리한다. `order_id`와 `product_order_id`는 서로 대체하지 않는다.

`AnswerResult`는 `status`, `category`, `reason`, `answer`, `provider`,
`auto_answerable`, `needs_review`, `matched_rule`, `warnings`, `metadata`를
갖는다. 상태는 `GENERATED`, `NEEDS_REVIEW`, `NOT_SUPPORTED`, `FAILED`다.
빈 답변은 성공으로 처리하지 않는다.

## 생성, 저장 및 workflow

`services/answer_service.py`는 inquiry 조회, 요청 변환, 엔진 실행, 초안 저장,
진행 단계와 활동 로그 갱신을 담당한다.

`answer_drafts` 저장 정책:

- 등록 전 재생성할 때마다 새 초안 행을 추가하고 이전 이력을 보존한다.
- 최신 초안과 전체 이력을 조회할 수 있다.
- 프로그램 답변은 `original_answer`에 저장한다.
- 이번 단계에서 `edited_answer`와 `final_answer`는 `NULL`이다.
- 수정본·최종본·검토 상태 저장 메서드는 준비했다.
- `posted` 초안 또는 `POSTED` 문의는 재생성과 변경을 차단한다.
- 문의 삭제 시 기존 외래키 정책에 따라 초안도 cascade 삭제된다.

기존 schema가 이 정책과 필요한 필드를 모두 지원하므로 migration version 2는
추가하지 않았으며 version 1도 수정하지 않았다.

상태 전환:

- 시작: `ANSWER_GENERATED → RUNNING`
- 정상 생성: 단계 `COMPLETED`, 문의 `REVIEW_PENDING`
- 검토 필요 또는 미지원: 단계 `NEEDS_REVIEW`, 문의 `NEEDS_ATTENTION`
- 실패: 단계 `FAILED`, 문의 `NEEDS_ATTENTION`

등록 전 재생성은 `ANSWER_GENERATED` 완료 단계를 명시적으로 다시 시작하고
시도 횟수를 증가시킨다. 다른 완료 단계의 전환 규칙은 완화하지 않았다.

## UI

문의 상세 영역에 `답변 초안 생성` 버튼을 추가했다. 상태, 분류, 자동답변
가능 여부, 검토 필요 여부, 판단 사유, provider, 프로그램 원본 답변, 경고,
생성 시각을 읽기 전용으로 표시한다. DB나 설정 오류가 발생해도 기존 문의
화면은 계속 동작한다.

실제등록, 학습 후보 저장, 학습규칙 반영, OpenAI 재생성 버튼은 없다.

## 원본 재현

개인정보가 없는 대표 샘플 10건에 대해 원본의 OpenAI 판단기를 메모리에서
비활성화한 후 안전하게 결과를 추출해
`tests/fixtures/auto_qna_characterization.json`에 고정했다. 배송, 설치,
색상, 모델·옵션, 미지원, 빈 질문, 특수문자·여러 줄, 상품명 누락, 검토 필요
사례를 포함한다.

10건 모두 답변 본문, 원본 분류, 판단 사유, 원본 상태, 질문 수가 일치했다.
의도적인 경계 계층 차이는 다음과 같다.

- 안전한 영문 상태 enum과 자동답변·검토 플래그 추가
- GPT 재판정과 네트워크 override 제거
- Excel 및 실행 결과 파일 쓰기 제거
- 미지원과 빈 답변을 명시적 상태로 변환
- 경고와 원본 상태를 metadata로 제공

원본 답변 문구와 분류 규칙은 임의로 개선하지 않았다.

## 테스트와 실행

```powershell
Set-Location -LiteralPath "C:\path\to\qa-auto"
python -m pytest -ra --basetemp ".pytest_tmp_manual"
python -m compileall .
python -m streamlit run "app.py"
```

테스트는 모델, 설정, 원본 fixture 비교, 저장 이력, workflow·로그·실패 격리,
adapter 및 UI 표시 모델을 다룬다. 실제 네이버 API, DPS, Chrome, OTP 및
OpenAI API는 호출하지 않는다.

## 다음 단계 DPS 연결 위치

`services/answer_service.py`에서 inquiry를 `AnswerRequest`로 변환한 직후,
`engine.generate(request)` 호출 전에 DPS 결과를 주입한다. 별도 adapter가
`dps_results`의 유효 결과를 `AnswerRequest.metadata["dps_result"]` 같은
엔진 중립 컨텍스트로 변환해야 한다.

엔진이 DB나 DPS Agent를 직접 import하지 않도록 유지한다. DPS에는
`product_order_id`가 아니라 일반 `order_id`만 전달하고, 설치·배송 규칙이
컨텍스트를 선택적으로 소비하도록 확장한다.

## 의도적으로 하지 않은 작업

- 네이버 실제 답변 등록과 실제등록 버튼
- OpenAI API 실제 호출
- DPS 자동조회 연결
- 직원 수정 UI, 학습 후보 저장 및 학습규칙 자동 반영
- 최종 현황·진행 카드 UI
- OTP, DPS 로그인 및 기존 네이버 인증 구조 변경

현재 기능은 답변 초안을 생성·저장·표시하는 단계까지만 지원한다.
