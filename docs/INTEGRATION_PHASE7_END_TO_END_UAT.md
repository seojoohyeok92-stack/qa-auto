# Q&A auto Phase 7: End-to-End UAT와 개발 PC 운영 준비

## 목적과 범위

Phase 7은 현재 Windows 개발 PC에서 다음 흐름을 사람이 직접 확인하기 위한
진단·감사 계층이다.

```text
네이버 문의 동기화 → 주문 확인 → 선택적 DPS 조회
→ Rule/Facts/Provider/Validator 확인 → Staff Edit → 승인 → Final Answer
```

서버 PC 이전, 시작·종료 배치, Windows 자동 시작, 외부 접속, 방화벽, 고정
IP, 프록시, 원격 접속 및 클라우드 배포는 포함하지 않는다. 네이버 실제 답변
등록도 잠금 상태다.

## 구조

- `services/environment_validation_service.py`: 값 없는 환경설정 검사
- `services/env_comparison_service.py`: `.env` 비교, 선택 병합·백업·rollback
- `services/uat_sync_service.py`: 스토어 격리형 문의 동기화
- `services/uat_order_service.py`: 문의별 네이버 주문 조회와 PII 제거
- `services/uat_diagnostic_service.py`: 14단계 UAT 상태
- `services/local_auth_service.py`: bcrypt 기반 로컬 역할 인증
- `services/quality_metrics_service.py`: 정답 점수가 아닌 수정 보조지표
- `ui/uat_panel.py`: UAT 점검 화면
- `ui/inquiry_uat_panel.py`: 문의·주문·Facts 상세 진단

기존 Rule, Hybrid, Governance, DPS, Approval 서비스는 그대로 사용하며 UAT
계층이 이를 우회하지 않는다.

## 실행 흐름

1. 프로젝트 루트에서 Streamlit을 실행한다.
2. 사이드바의 `UAT 점검`을 연다.
3. 로컬 항목을 확인한다.
4. 필요할 때만 DPS Agent 상태 확인을 누른다.
5. 조회 기간과 답변 상태를 선택하고 `문의 동기화`를 누른다.
6. Dashboard에서 문의를 선택한다.
7. 문의·주문·Facts UAT expander에서 네이버 주문정보를 확인한다.
8. 배송·설치 문의이면 일반 `order_id`로만 DPS를 조회한다.
9. 답변 출처와 외부 AI 호출 여부를 확인한다.
10. Staff Edit을 저장하고 권한이 있는 사용자가 승인한다.
11. Final Answer와 Activity Log를 확인한다.

## 사용자 역할

- `ADMIN`: 설정/UAT/env 비교/사용자/전체 로그/승인/통계
- `MANAGER`: 문의 검토, Staff Edit, 승인·반려, 제한된 로그와 통계
- `AGENT`: 문의, DPS, Program Answer, Staff Edit, 승인 요청

기본값은 기존 UI를 보존하기 위해 로컬 인증 비활성 개발 ADMIN 문맥이다.
인증을 사용할 때만 `QNA_LOCAL_AUTH_ENABLED=true`로 설정한다.

초기 ADMIN:

```powershell
python `
  "scripts\manage_local_user.py" `
  --username "admin" --display-name "관리자"
```

비밀번호 변경:

```powershell
python `
  "scripts\manage_local_user.py" `
  --username "admin" --change-password
```

비밀번호는 명령행 인자로 받지 않고 `getpass`로 입력하며 DB에는 bcrypt
hash만 저장한다.

## 환경설정 검사

Application, Database, Naver Store, DPS Agent, GPT Governance, Privacy,
Approval, Activity Log, UAT Mode 영역을 검사한다. 결과에는 변수명, 존재,
형식, 적용 영역, 설명, 조치만 포함되고 값은 포함되지 않는다.

현재 개발 PC 검사에서는 두 스토어의 ID/Secret 존재와 형식은 정상으로
확인됐다. `QNA_GPT_PRIVACY_ENABLED`가 `.env`에 명시되지 않아 `미설정`
주의로 표시된다. 코드 기본값은 Privacy 활성이나 운영 가시성을 위해 명시
설정을 권고한다. 외부 GPT 승인과 API key는 미설정이며 FAKE 사용은 가능하다.

## .env 비교와 Migration

UAT 화면은 읽기 전용 보고서만 제공한다.

- `SAME`, `DIFFERENT`
- `CURRENT_ONLY`, `COMPARED_ONLY`
- `PRESENT`, `EMPTY`, `MISSING`
- `REQUIRED`, `CONDITIONAL`, `OPTIONAL`, `DEPRECATED`, `UNKNOWN`

값은 UI와 감사 이력에 저장하지 않는다. 파일은 UTF-8 BOM, UTF-8, CP949
순으로 텍스트 파싱하며 파싱 불가능한 파일은 오류로 구분한다. AnySign4PC
아이콘이나 Windows 파일 연결만으로 암호화 여부를 판단하지 않는다.

서비스 계층의 선택 병합은 기본적으로 기존 값을 덮어쓰지 않는다. 실행 전
같은 폴더에 timestamp 백업을 만들고 rollback할 수 있으며 변수명과 작업
결과만 Activity Log에 남긴다.

## 네이버 실제 문의 테스트

활성화되고 Client ID/Secret이 완성된 스토어만 조회한다. 토큰, 상품문의,
고객문의 오류는 스토어별로 격리한다. 동기화는 기존 upsert를 사용하므로
Staff Edit, 승인, posted, DPS 및 답변 이력은 초기화하지 않는다.

2026-07-29 개발 PC 실제 결과:

- 설정 스토어: 2
- 성공 스토어: 2
- 조회 문의: 200
- 신규: 4
- 갱신: 39
- 변경 없음: 157
- 실패: 0

동기화 전 200건은 삭제되지 않았고 결과적으로 204건이 저장됐다.

## 주문정보 테스트

네이버 주문 조회는 일반 주문번호와 상품주문번호를 모두 구분해 사용할 수
있다. DPS는 기존 정책대로 일반 `order_id`만 사용하며 product order
fallback은 없다.

실제 문의 한 건을 일반 주문번호로 재조회한 결과 `ORDER_ID`, 1건,
`DELIVERING` 상태가 확인됐다. UAT 결과에서 수취인 이름, 전화번호, 주소 및
배송메모는 제거됐다.

## DPS 테스트

UAT 상태는 다음 오류를 구분한다.

```text
AGENT_OFFLINE, AGENT_TIMEOUT, DPS_LOGIN_REQUIRED, CHROME_NOT_FOUND,
DPS_TAB_NOT_FOUND, ORDER_INPUT_NOT_FOUND, QUERY_CONTROL_NOT_FOUND,
ORDER_NOT_FOUND, MULTIPLE_RESULTS, DETAIL_OPEN_FAILED,
DETAIL_PARSE_FAILED, WINDOW_RESTORE_FAILED, UNKNOWN
```

개발 PC 확인 시 Agent는 실행 중이고 mode는
`WINDOWS_UI_AUTOMATION_TAB_V6_LOGIN_NAV`였으나 로그인 상태가
`DPS_TAB_NOT_FOUND`였다. 따라서 실제 주문 DPS 조회와 상세창 복귀 검증은
실행하지 않았다.

## Rule, Fake, 실제 GPT 구분

UI는 다음 출처를 사용한다.

```text
RULE
FAKE_PROVIDER
OPENAI_SHADOW
OPENAI_CANARY
OPENAI_ACTIVE
RULE_FALLBACK
```

Fake와 Rule에는 `실제 외부 AI 호출 없음`을 표시한다. SHADOW 결과는 비교
전용이며 Program Answer는 Rule임을 함께 표시한다.

## 실제 OpenAI Transport

회사 승인, Privacy, mode, 허용 provider/model, API key, Rate/Cost Gate가
통과된 후에만 `OpenAIResponsesTransport`가 생성된다. OpenAI Responses API
JSON object 계약을 사용하며 HTTP/JSON 오류는 기존 retry/fallback 정책으로
전달된다. 기본 테스트는 주입한 mock session만 사용한다.

공식 참고:

- https://platform.openai.com/docs/api-reference/responses
- https://developers.openai.com/api/docs/guides/structured-outputs

현재 개발 PC에는 회사 승인과 API key가 없으므로 실제 외부 호출은 0회다.

## 승인 흐름과 품질 보조지표

```text
Program Answer → Staff Edit → 승인 → Final Answer
```

posted 상태는 기존 보호 정책을 유지한다. 승인 history에는 actor를 기록한다.
Staff Edit/승인 시 문자·단어 변경 비율, 문장 추가·삭제, 사실/금지표현/말투
변경, 수정 시간, 승인, 재생성 횟수를 별도 저장한다. 이 수치는 정답 점수가
아닌 `수정 지표`다.

## 장애와 개인정보 정책

사용자 메시지는 실패 작업, 보존 데이터, 조치, 재시도 가능성을 설명한다.
기술 details는 error code, correlation ID, component, operation, exception
type, elapsed time만 사용한다.

API key, Secret, token, Cookie, Session, OTP, Prompt 전문, 외부 GPT 응답
전문, 주문 수취인 정보는 UAT 이력에 저장하지 않는다.

## 테스트와 Windows 검증

- 변경 전: `538 passed`
- Phase 7 신규: `117`개
- 변경 후 전체: `655 passed`
- Streamlit AppTest: Dashboard와 UAT 예외 0건
- API key 없음 + FAKE: Validator 통과, Staff Edit, 승인, Final 생성
- 실제 네이버 등록: 실행하지 않음

## 알려진 제한과 다음 단계

- DPS 탭이 열려 있지 않아 실조회는 미검증
- 로컬 인증은 기업 SSO가 아님
- Shadow 호출은 background queue가 아닌 동기 흐름
- SQLite rate limit은 분산 서버용 lock이 아님
- 한국어 자유문장 PII는 추가 DLP 검토 필요
- 실제 OpenAI SHADOW는 회사 승인 전까지 미검증

다음 단계는 개발 PC UAT에서 DPS 탭/로그인 상태를 준비해 실제 1건을
확인하고, 회사 승인 후에만 OpenAI SHADOW 1건을 수동 검증하는 것이다.
네이버 실제 답변 등록은 별도 Gate 단계로 유지한다.
