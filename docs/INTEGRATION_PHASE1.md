# Q&A auto 통합 1단계

## 프로젝트와 작업 범위

프로젝트명은 `Q&A auto`이다.

- 기존 네이버/DPS 원본: `C:\path\to\legacy-naver-dps`
- 자동 Q&A 원본: `C:\path\to\legacy-auto-qna`
- 작업용 복사본: `C:\path\to\qa-auto`

실제 전달 입력은 두 개의 ZIP이었다. 원본 ZIP의 SHA-256은 다음과 같다.

- `naver_tv_bot.zip`: `60336F40BB5560A7E3900BFF9B2C7F46A5760F1786D33079FA255346AC64156C`
- 자동 Q&A ZIP: `929182736C056AA867C00F97A89327DD6BD3F124B38F147A236EFC6C4083FFF1`

ZIP은 동일 상위 경로에 원본 폴더로 보존 해제했다. 그 후
`naver_tv_bot`의 포함 대상 59개 파일을 상대 경로와 파일 해시 기준으로
검증하여 `Q&A auto`를 만들었다. 기존 `Q&A auto` 폴더는 없었으므로
백업은 생성하지 않았다.

복사본에는 다음 실행환경 또는 민감 산출물을 포함하지 않았다.

- `.env`
- `.venv`, `venv`
- 모든 `__pycache__`, `*.pyc`, `*.pyo`
- `logs`, `cache`
- `*.log`, `*.db`, `*.sqlite`, `*.sqlite3`
- `data/dps_agent_state.json`
- `data/dps_connection.json`
- `data/dps_windows_cache.json`

자동 Q&A 원본의 코드, 실행 파일, 출력, 학습자료, 설정, 환경 파일은 이번
단계에서 작업 복사본으로 이식하지 않았다.

## 변경 파일

추가:

- `repositories/__init__.py`
- `repositories/database.py`
- `repositories/inquiry_repository.py`
- `repositories/workflow_repository.py`
- `repositories/log_repository.py`
- `workflow/__init__.py`
- `workflow/models.py`
- `services/inquiry_sync_service.py`
- `tests/test_phase1_repositories.py`
- `tests/test_inquiry_sync_service.py`
- `docs/INTEGRATION_PHASE1.md`
- `README.md`
- `.gitignore`

수정:

- `app.py`
- `ui/sidebar.py`
- `ui/dashboard.css`

기존 DPS 자동화, DPS Agent, 네이버 API 모듈과
`services/work_queue_service.py`의 공개 반환 구조는 변경하지 않았다.

## DB 경로와 연결 설정

기본 SQLite 경로는 프로젝트 기준 `data/oje_automation.db`이다.
환경변수 `OJE_AUTOMATION_DB_PATH`로 변경할 수 있다.

연결은 다음 안전 설정을 적용한다.

- DB 상위 폴더 자동 생성
- WAL journal mode
- `foreign_keys = ON`
- 5초 `busy_timeout`
- `sqlite3.Row` row factory
- 명시적 트랜잭션 commit/rollback
- 사용 후 연결 종료
- 모든 업무 값의 파라미터 바인딩

`schema_migrations`가 적용 버전을 기록한다. 현재 스키마 버전은 1이며,
같은 마이그레이션은 다시 적용되지 않는다.

## DB 테이블

- `schema_migrations`: 적용 버전과 적용 시각
- `inquiries`: 스토어/출처/원본 질문 ID, 질문·상품·주문 식별자,
  업무 상태와 원본 JSON. `(store_code, source_type,
  source_question_id)`가 유일 키이다.
- `workflow_steps`: 문의별 단계 상태, 시작/완료 시각, 시도 횟수,
  최근 오류와 metadata. 문의 삭제 시 cascade 삭제된다.
- `activity_logs`: 문의 또는 시스템 활동. 문의 삭제 시 로그의
  `inquiry_id`만 `NULL`로 바뀐다.
- `dps_results`: 일반 `order_id` 기준 DPS 결과와 만료 시각.
  문의 삭제 시 cascade 삭제된다.
- `answer_drafts`: 원본/직원 수정/최종 답변과 검토·등록 상태.
  문의 삭제 시 cascade 삭제된다.
- `learning_candidates`: 답변 수정 학습 후보와 검수/반영 상태.
  문의 삭제 시 cascade 삭제되고, 답변 초안만 삭제되면
  `answer_draft_id`는 `NULL`이 된다.

문의 상태, 등록일, 주문번호, 단계 상태, 활동 시각,
`(store_code, order_id)`, DPS 만료 시각 등에 인덱스를 둔다.

DB에는 비밀번호, API secret, access/refresh token, OTP, Authorization,
API key 형태의 필드를 원본 JSON으로 저장하지 않는다.

## 문의와 진행 상태

문의 상태:

`NEW`, `ANALYZING`, `ORDER_PENDING`, `DPS_PENDING`,
`ANSWER_PENDING`, `REVIEW_PENDING`, `READY_TO_POST`, `POSTED`,
`NEEDS_ATTENTION`, `FAILED`

진행 단계의 단일 기본 순서:

1. `INQUIRY_COLLECTED`
2. `QUESTION_ANALYZED`
3. `ORDER_IDENTIFIED`
4. `NAVER_ORDER_LOOKUP`
5. `DPS_LOOKUP`
6. `ANSWER_GENERATED`
7. `STAFF_REVIEW`
8. `NAVER_POST`
9. `LEARNING_SAVED`

단계 상태:

`PENDING`, `RUNNING`, `COMPLETED`, `NEEDS_REVIEW`, `FAILED`,
`SKIPPED`

enum 검증, DB `CHECK` 제약, 전환 규칙을 함께 적용한다. 실패나 검토 필요
상태에서 재시도하면 `RUNNING`으로 바뀌고 `attempt_count`가 증가한다.
완료 또는 건너뜀 상태는 다시 시작할 수 없다.

## 기존 문의 동기화

`InquirySyncService`는 기존 `load_work_queue()` 반환 튜플 또는 work item
목록을 받는다. 기존 공개 반환 구조는 변경하지 않는다.

각 항목은 다음과 같이 처리한다.

1. 스토어, 출처, 원본 질문 ID와 업무 필드를 정규화한다.
2. 원본 질문 ID는 기존 최상위 키가 없을 때
   `original_data.questionId` 또는 `original_data.inquiryNo`에서 복구한다.
3. 유일 키로 inquiry를 upsert한다.
4. 신규 문의만 9개 단계를 초기화하고 `INQUIRY_COLLECTED`를 완료한다.
5. 재수집 시 누락된 단계만 `INSERT OR IGNORE`로 복구하고 수집 단계가
   아직 `PENDING`인 경우만 완료한다.
6. 재수집된 문의는 원본 업무 필드만 갱신한다. 직원 workflow 상태,
   답변 상태, 단계, 답변 초안과 학습 후보는 보존한다.
7. 개별 실패는 마스킹된 활동 로그를 남기고 다음 항목을 계속 처리한다.
8. `new`, `updated`, `unchanged`, `failed` 집계를 반환한다.

활동 로그는 전화번호, 이메일, 긴 주문번호 일부, 지정된 고객 이름,
Bearer token, secret/API key/OTP 형태를 저장 전에 마스킹한다. 원본
문의 데이터 자체를 로그 마스킹 과정에서 변경하지 않는다.

## 앱 연결

Streamlit 페이지 제목과 사이드바 프로젝트명을 `Q&A auto`로 변경했다.
앱 시작 시 스키마 초기화를 시도한다. 기존 문의 로드가 성공하면 DB
동기화를 실행한다.

사이드바의 `관리자 진단`에는 프로젝트명, 절대 DB 경로, DB 상태,
저장 문의 수, 최근 동기화 집계를 표시한다. DB 초기화나 동기화 실패는
기술 로그에 traceback을 기록하되 기존 문의 화면을 중단하지 않는다.

## 실행

현재 PC는 WindowsApps의 `python` 실행 별칭이 실제 Python보다 PATH
앞에 있어 단순 `python` 호출이 실패한다. 현재 확인된 Python 3.14
실행 파일을 사용하면 다음 명령이 그대로 동작한다.

```powershell
Set-Location -LiteralPath "C:\path\to\qa-auto"
python -m streamlit run "app.py"
```

DB 경로를 바꾸는 예:

```powershell
Set-Location -LiteralPath "C:\path\to\qa-auto"
$env:OJE_AUTOMATION_DB_PATH = ".\data\custom_oje_automation.db"
python -m streamlit run "app.py"
```

DPS Agent의 기존 진입점:

```powershell
Set-Location -LiteralPath "C:\path\to\qa-auto"
python -m dps.agent_server
```

## 테스트와 컴파일

pytest 기본 임시 경로에 현재 계정의 접근 제한이 있으므로 프로젝트 안에
명시적 임시 경로를 사용한다.

```powershell
Set-Location -LiteralPath "C:\path\to\qa-auto"
python -m pytest -ra --basetemp ".pytest_tmp_manual"
python -m compileall "app.py" "config.py" "main.py" "ai" "api" "core" "dps" "repositories" "scripts" "services" "tests" "ui" "workflow"
```

변경 전 기존 테스트는 221개가 통과했다. 변경 후 기존 221개와 신규
29개를 합친 250개가 모두 통과했다. 외부 네이버 API, Chrome, DPS,
OTP는 신규 테스트에서 호출하지 않는다.

## DB 초기화

앱과 DPS 관련 프로세스를 먼저 종료한 뒤 기존 DB를 보존 이름으로
옮기고 앱을 다시 시작하면 새 DB가 자동 생성된다. WAL과 SHM 파일이
있다면 같은 시각 접미사로 함께 보존한다.

```powershell
Set-Location -LiteralPath "C:\path\to\qa-auto"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
foreach ($suffix in @("", "-wal", "-shm")) {
    $sourcePath = ".\data\oje_automation.db$suffix"
    $backupPath = ".\data\oje_automation_backup_$stamp.db$suffix"
    if (Test-Path -LiteralPath "$sourcePath") {
        Move-Item -LiteralPath "$sourcePath" -Destination "$backupPath"
    }
}
python -m streamlit run "app.py"
```

## 원본으로 되돌리기

원본 두 폴더와 ZIP은 수정하지 않았다. 문제가 생기면 현재
`Q&A auto`를 타임스탬프 백업 이름으로 변경한 뒤, 원본
`naver_tv_bot`에서 새 복사본을 만들 수 있다. `.env`, 로그, 캐시,
가상환경, DPS 상태 파일은 다시 복사하지 않는다.

```powershell
Set-Location -LiteralPath "C:\path\to"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
Move-Item -LiteralPath ".\Q&A auto" -Destination ".\Q&A auto_backup_$stamp"
& "C:\Windows\System32\Robocopy.exe" ".\naver_tv_bot" ".\Q&A auto" /E /COPY:DAT /DCOPY:T /R:1 /W:1 /XD "__pycache__" ".venv" "venv" "logs" "cache" /XF ".env" "*.pyc" "*.pyo" "*.log" "*.db" "*.sqlite" "*.sqlite3" "dps_agent_state.json" "dps_connection.json" "dps_windows_cache.json"
```

robocopy 종료 코드 0~7은 정상 범위이며 8 이상이면 복구 실패로 본다.

## 다음 단계 이식 위치

자동 Q&A 원본에서 다음 모듈을 역할별로 검토한 뒤 선택 이식한다.
이번 단계에서는 복사하거나 import 연결하지 않았다.

- 답변 생성: `qna_auto/engine.py`
- 답변 형식: `qna_auto/answer_format.py`
- 텍스트 처리: `qna_auto/text_utils.py`
- 기본 설정: `qna_auto/config.py`
- Excel 설정: `qna_auto/config_excel.py`
- OpenAI 판정: `qna_auto/openai_judge.py`
- 학습 후보/이력: `qna_auto/learning.py`
- 네이버 클라이언트: `qna_auto/naver_client.py`
- 네이버 답변 workflow: `qna_auto/naver_workflow.py`
- 정책 설정: `qna_auto_configs/`
- 학습자료: `학습자료/model_data_with_color.json`

연결 지점은 `answer_drafts`와 `learning_candidates` repository 계층,
`ANSWER_GENERATED`, `STAFF_REVIEW`, `NAVER_POST`, `LEARNING_SAVED`
단계이다. 실제 등록 기능을 연결하기 전에는 `NAVER_POST`를 실행하지
않는다.

DPS 조회에는 반드시 일반 `order_id`만 전달한다. 네이버 API 내부의
기존 상품주문번호 조회 기능은 유지하지만 `product_order_id`를 DPS
입력값으로 사용하지 않는다.

## 의도적으로 하지 않은 작업

- 자동 Q&A 답변 엔진 또는 설정/학습자료 이식
- OpenAI API 호출
- 네이버 실제 답변 등록과 실제등록 버튼
- 학습규칙 자동 반영
- 신규 현황/진행 카드 UI
- DPS Agent 리팩터링, 로그인/OTP 변경
- 네이버 인증 구조 또는 클라이언트 교체
- 과거 output, EXE, 로그, 실행 결과 이관
- `.env`, `GPT API.env`, 인증 토큰 통합

`.env`와 인증정보는 `Q&A auto`에 복사하지 않았고 이 문서에도 실제
값을 기록하지 않았다.
