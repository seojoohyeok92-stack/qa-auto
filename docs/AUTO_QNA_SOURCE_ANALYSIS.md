# 자동 Q&A 원본 분석

## 분석 범위와 원칙

- 분석 원본:
  `C:\path\to\legacy-auto-qna`
- 이식 대상:
  `C:\path\to\qa-auto`
- 분석 시 원본 파일은 읽기만 했으며 수정하거나 생성하지 않았다.
- 실제 네이버 API와 OpenAI API는 호출하지 않았다.
- 원본 답변 결과 확인 시 `OpenAIJudge`를 생성 전에 메모리에서
  비활성 대체하여 `.env`/`GPT API.env` 로드와 네트워크 요청을 막았다.
- 개인정보가 없는 10개 대표 질문만 사용했다.

## 파일별 분석

| 원본 파일 | 역할 | 주요 입력 | 주요 반환 | 외부 파일 | 환경변수 | 네이버 API | OpenAI | 파일 쓰기 부수효과 | 2단계 이식 | 이식 위치와 변경 이유 |
|---|---|---|---|---|---|---|---|---|---|---|
| `qna_auto/engine.py` | 규칙 순서, 분류, 답변 후보 생성 | 상품명, 질문, 옵션명, `AppConfig` | 원본 `AnswerResult` | JSON 설정, `configuration.xlsx`, 모델 학습 JSON | 간접적으로 OpenAI 환경값 | 없음 | `OpenAIJudge`를 간접 호출 | 직접 쓰기는 없으나 생성자가 설정 workbook 생성을 유발할 수 있음 | 예, 핵심 규칙만 선택 이식 | `answer/engine.py`; DB/UI/API 의존성을 제거하고 새 `AnswerRequest/AnswerResult`와 provider 인터페이스를 사용 |
| `qna_auto/answer_format.py` | 인사말, 본문, 톡톡 안내, 맺음말 조합 | 답변 본문과 선택 문구 | 최종 답변 문자열 | 없음 | 없음 | 없음 | 없음 | 없음 | 예 | `answer/answer_format.py`; 원문 형식을 유지 |
| `qna_auto/text_utils.py` | 공백 압축, 비교용 compact, 키워드/날짜 힌트 | 임의 텍스트 | 정규화 문자열 또는 bool | 없음 | 없음 | 없음 | 없음 | 없음 | 예, 확장 | `answer/text_utils.py`; 원본 함수 유지 후 줄바꿈·상품/옵션 정리와 로그/표시용 개인정보 마스킹을 추가 |
| `qna_auto/config.py` | JSON 설정과 Excel override 조합 | config 경로 | `AppConfig` | 5개 JSON, `configuration.xlsx` | `QNA_CONFIG_DIR` | 없음 | 설정만 포함 | `ensure_configuration_workbook()` 때문에 파일이 없으면 생성 | 직접 이식하지 않음 | `answer/config_loader.py`로 읽기 전용 재구현. 실행 위치와 무관한 `Path(__file__)` 기반 경로 사용 |
| `qna_auto/config_excel.py` | 설정 workbook 생성/override, 상품·학습·설치 규칙 로드 및 상품코드 추가 | 프로젝트 root, workbook, record | dict/list/Path | `configuration.xlsx`, 모델 JSON | 없음 | 없음 | 없음 | workbook 생성·수정·저장 가능 | 직접 이식하지 않음 | 설정 override와 상품DB는 원본 JSON과 동일하므로 제외. 필요한 설치 일정 9개만 정적 JSON으로 추출 |
| `qna_auto/openai_judge.py` | 질문 분리, GPT 판정 프롬프트, Responses API 호출 | 상품, 질문, 규칙 후보 | GPT JSON 판단 | `.env`, `GPT API.env`, `qna_auto/.env` | `OPENAI_API_KEY`, `OPENAI_MODEL` | 없음 | 있음 | 파일 쓰기는 없지만 환경변수를 process에 주입 | 네트워크 코드는 이식하지 않음 | `answer/providers/openai_provider.py`는 명시적 비활성 provider. 질문 수 추정은 순수 함수로 이식 |
| `qna_auto/learning.py` | 답변 이력, 학습 후보, 검수 workbook, 채택 규칙 반영 | 답변 record/history/review rows | 후보 dict 또는 Path | `outputs/learning`, `configuration.xlsx` | 없음 | 없음 | 없음 | JSONL/XLSX 생성 및 `configuration.xlsx` 변경 | 미이식 | 이번 단계는 학습 저장/반영 범위가 아님. 후속 단계에서 `learning_candidates`와 연결 |
| `qna_auto/naver_client.py` | 인증, 문의 조회/상세/등록, 원본 응답 정규화 | 환경 인증값, API 요청/응답 | `NaverQuestion`, HTTP 결과 | `.env`, `GPT API.env` | 네이버 client ID/secret 등 | 있음 | 없음 | 파일 쓰기는 없으나 env 파일을 읽어 process 환경을 변경 | 미이식 | 기존 `Q&A auto`의 `api/`와 `services/work_queue_service.py`를 유지. 필요한 입력 변환은 `answer/source_adapter.py`로 독립 구현 |
| `qna_auto/naver_workflow.py` | 네이버 수집→답변→등록→보고서→학습 이력 orchestration | `NaverRunOptions` | 출력 XLSX Path | outputs, learning history, configuration workbook | 네이버 환경값 | 조회 및 실제 등록 가능 | 엔진을 통해 가능 | 보고서/XLSX/JSONL/config workbook 쓰기 | 미이식 | 실제 등록과 output/학습은 이번 단계 범위 밖. orchestration은 `services/answer_service.py`로 새로 구성 |
| `qna_auto_configs/answer_policy.json` | hard block 규칙 | 규칙 키워드/사유 | 설정 dict | 자체 파일 | 없음 | 없음 | 없음 | 없음 | 복사 | `answer_data/configs/answer_policy.json` |
| `qna_auto_configs/shipping_config.json` | 택배·설치·수거·방문수령 문구 | 설정 key | 설정 dict | 자체 파일 | 없음 | 없음 | 없음 | 없음 | 복사 | `answer_data/configs/shipping_config.json` |
| `qna_auto_configs/event_config.json` | 리뷰/온누리 행사 규칙과 문구 | 설정 key | 설정 dict | 자체 파일 | 없음 | 없음 | 없음 | 없음 | 복사 | `answer_data/configs/event_config.json` |
| `qna_auto_configs/model_codes.json` | 스탠드·배터리·모델 코드 문구 | 설정 key | 설정 dict | 자체 파일 | 없음 | 없음 | 없음 | 없음 | 복사 | `answer_data/configs/model_codes.json` |
| `qna_auto_configs/openai_config.json` | OpenAI 활성/모델/timeout/confidence | 설정 key | 설정 dict | 자체 파일 | API key는 없음 | 없음 | 설정상 활성 | 없음 | 미복사 | 기본 provider가 rule이므로 불필요. OpenAI 비활성을 코드로 명시 |
| `qna_auto_configs/configuration.xlsx` | JSON override, 상품DB, 상품코드, 학습룰, 설치일정 | workbook sheet | 설정과 규칙 목록 | 자체 workbook | 없음 | 없음 | 설정 sheet 포함 | 원본 도구에서 수정 가능 | 미복사 | JSON override 40개는 원본 JSON과 전부 동일하고 상품DB 1,586개도 모델 JSON과 동일. 학습룰은 0개. 설치일정 9개만 `install_schedule_rules.json`으로 안전하게 추출 |
| `학습자료/model_data_with_color.json` | 1,586개 모델 catalog와 관련 정적 catalog | 모델 코드 | 모델 dict | 자체 파일 | 없음 | 없음 | 없음 | 없음 | 복사 | `answer_data/learning/model_data_with_color.json` |

## 설정과 데이터 의존성 결론

원본 `configuration.xlsx`를 읽기 전용으로 비교한 결과:

- `설정_JSON`: 40개 override가 각 원본 JSON 값과 모두 동일
- `상품DB`: 1,586개 모델이며 모델 학습 JSON의 엔진 사용 필드와 모두 동일
- `학습답변룰`: 활성/비활성 포함 데이터 행 0개
- `설치배송일정`: 활성 규칙 9개
- `상품코드DB`: 2개 상품 식별자 행이 있으나 답변 엔진에는 사용되지 않음

따라서 workbook을 복사하지 않고 설치 일정 9개만
`answer_data/configs/install_schedule_rules.json`으로 옮긴다. 이는
workbook 생성/수정 부수효과와 `openpyxl` 런타임 의존성을 제거하면서
현재 원본 규칙 결과를 보존한다.

설정과 workbook에서 고객 이메일, 고객 휴대전화, 인증 token/secret,
긴 고객 주문번호 패턴은 발견되지 않았다. 설치 일정과 일부 엔진
문구에는 원본에 존재하던 공개 사업자/제조사 상담 전화번호가 있다.
이는 고객 개인정보가 아니며 원본 결과 재현을 위해 유지한다. 신규
고객 연락처나 주문정보는 설정·fixture에 추가하지 않는다.

## import와 실행 부수효과

### 안전한 부분

- `answer_format.py`, `text_utils.py`는 순수 함수만 정의한다.
- `engine.py` 자체 import는 네트워크나 파일 쓰기를 즉시 실행하지 않는다.
- `naver_client.py`, `naver_workflow.py`, `learning.py`도 import만으로
  네트워크 요청이나 output 저장을 실행하지 않는다.

### 격리가 필요한 부분

- 원본 `AnswerEngine()` 생성은 `load_config()`를 호출한다.
- `load_config()`는 `configuration.xlsx`가 없으면 새로 생성한다.
- 원본 `OpenAIJudge()` 생성은 `.env`, `GPT API.env`,
  `qna_auto/.env` 값을 process 환경에 주입한다.
- API key가 존재하면 `AnswerEngine.answer()`의 finalize 단계에서
  OpenAI Responses API를 호출할 수 있다.
- `naver_workflow` 실행 함수는 네이버 조회/등록, 결과 XLSX,
  답변 이력 JSONL과 configuration workbook 변경을 수행할 수 있다.
- `learning`의 append/apply 함수는 학습 XLSX/JSONL 및 설정 workbook을
  생성하거나 변경한다.

새 엔진은 위 동작을 포함하지 않는다. config loader는 읽기 전용이고,
기본 provider는 항상 `RuleProvider`이며, 비활성 OpenAI provider는
호출 시 명시적 예외만 반환한다.

## 원본 characterization 기준

원본 소스는 다음 안전 장치로 결과만 추출했다.

1. 원본 경로를 Python import path에 읽기 전용으로 추가
2. `qna_auto.openai_judge.OpenAIJudge`를 생성 전에 메모리상
   `available() == False`인 대체 객체로 교체
3. `.env`/`GPT API.env` 미로드, 네트워크 호출 금지
4. 기존 `configuration.xlsx`는 읽기만 수행
5. 개인정보가 없는 10개 질문 결과를 고정 fixture의 근거로 사용

범주는 일반 상품, 택배 배송, 기사 설치, 색상, 모델/옵션,
지원하지 않는 질문, 빈 질문, 특수문자/여러 줄, 상품명 누락,
직원 검토 필요 질문이다.

새 외부 모델은 원본 한글 status를 다음처럼 안전하게 매핑한다.

- 원본 `답변 가능` + 비어 있지 않은 답변 → `GENERATED`
- 원본 `추가정보 필요` → `NEEDS_REVIEW`
- 원본 `답변하지 않음` → `NEEDS_REVIEW` 또는 입력 누락 시
  `NOT_SUPPORTED`
- 엔진/설정 오류 → `FAILED`가 아니라 정의된 예외를 service에서
  `FAILED` 결과로 변환

답변 본문, category, reason, question count와 rule 판정 순서는 원본과
동일하게 유지한다. status 명칭과 검토 플래그는 통합 DB/UI를 위한
의도적 구조 차이이며 rule 결과 개선이 아니다.

## OpenAI 프롬프트 재사용 가능 요소

후속 단계에서 재사용할 수 있는 요소는 다음과 같다.

- 질문을 여러 줄/물음표/번호 목록으로 나누는 `estimate_question_count`
- 복합 질문 중 하나라도 주문별 확인이 필요하면 답변하지 않는 정책
- rule 후보의 status/category/reason/answer를 judge 입력으로 전달하는
  provider 인터페이스
- 구조화된 결과 필드:
  `question_count`, `questions`, `should_answer`, `answer_body`,
  `category`, `reason`, `confidence`
- 최소 confidence threshold

이번 단계에서는 프롬프트 전문과 HTTP 호출 코드를 실행 경로에
이식하지 않는다.
