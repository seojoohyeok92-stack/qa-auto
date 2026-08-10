# Q&A Auto

Q&A Auto는 네이버 문의를 동기화하고, 문의 분석·주문번호 판정·Samsung DPS 조회·답변 초안 생성·직원 검토·학습 이력을 하나의 Streamlit 운영 화면에서 관리하는 Windows용 Q&A 운영 도구입니다.

운영 데이터는 로컬 SQLite DB에 저장합니다. DB, 환경변수, 로그와 DPS 로컬 상태는 Git 저장소에 포함하지 않습니다.

## 주요 기능

- Dashboard 문의 목록, 검색·필터, 페이지 크기 10/15/20/30 및 선택 상태 유지
- 네이버 문의 Auto Sync와 수동 동기화
- 문의 분석과 `ProcessingPlan` 기반 답변 처리
- 배송·설치 일정 문의의 주문번호 판정과 DPS 조회
- Program Answer → 직원 수정 → Validator → 승인 → Final Answer
- Runtime Auto Post 제어와 안전 검증
- Historical Verified Learning, Positive Learning, Copilot Correction Learning
- Learning Context 안전필터, provenance 및 성과 화면
- GPT Copilot과 Project Knowledge
- DPS Session Monitor, 로그인 만료 감지 및 read-only Keepalive
- DPS 장애·로그인 만료 시 Auto Post 안전 보류

## 시스템 구성

```text
Naver API ──> Auto Sync ──> SQLite
                              │
Streamlit Dashboard ──> Inquiry Analysis ──> ProcessingPlan
                              │                    │
                              │                    └─> DPS Windows Agent
                              │                         └─> Chrome + pywinauto
                              └─> Answer Generation ──> Review/Validator
                                                       └─> Auto Post Gate
```

DPS는 HTTP API를 직접 호출하는 방식이 아니라, 로그인된 Windows Chrome의 DPS 탭을 `pywinauto`로 조작하는 구조입니다. DPS ID·비밀번호·cookie·token은 프로젝트가 저장하지 않습니다.

## 프로젝트 구조

| 경로 | 역할 |
|---|---|
| `app.py` | Streamlit 운영 Dashboard 진입점 |
| `dps/` | Windows DPS Agent, Chrome UI Automation, Session Monitor |
| `services/` | 동기화, 분석, 답변, 학습, Auto Post 서비스 |
| `repositories/` | SQLite repository와 Migration |
| `answer/`, `ai/` | 답변 정책, provider, validator |
| `ui/` | 기존 다크 Dashboard 및 관리 화면 |
| `answer_data/` | 실행에 필요한 정적 답변 설정·지식·모델 자료 |
| `tests/` | pytest 및 Streamlit AppTest |
| `scripts/` | 진단·검증·유지보수 도구 |
| `docs/` | 단계별 설계와 검증 기록 |

## 설치

Windows PowerShell에서 다음을 실행합니다.

```powershell
Set-Location -LiteralPath "C:\path\to\qa-auto"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

테스트와 XLSX 유지보수 도구가 필요하면 개발 의존성을 추가합니다.

```powershell
python -m pip install -r requirements-dev.txt
```

## 환경설정

`.env.example`을 `.env`로 복사하고 서버 PC에서 값을 입력합니다.

```powershell
Copy-Item .env.example .env
```

- `.env`는 Git에 포함되지 않습니다.
- Secret은 저장소나 문서에 기록하지 않습니다.
- 운영 DB는 기본 `data/oje_automation.db` 또는 `OJE_AUTOMATION_DB_PATH`로 지정한 별도 경로를 사용합니다.
- 초기 배포에서는 `NAVER_POST_ENABLED=false`, `NAVER_AUTO_POST_ENABLED=false`를 유지합니다.
- GPT 실제 provider는 회사 승인, 개인정보 보호 설정과 API key가 모두 준비된 경우에만 활성화합니다.

## 실행

DPS를 사용하는 서버에서는 동일한 Windows 로그인 세션에서 Chrome과 DPS Agent를 먼저 실행합니다.

```powershell
python -m dps.agent_server
```

별도 PowerShell에서 Streamlit을 실행합니다.

```powershell
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8502
```

사내 접속을 위해 `0.0.0.0`을 사용할 때는 방화벽, 인증, VPN 또는 reverse proxy 정책을 먼저 적용하십시오. 자세한 서버 절차는 [DEPLOYMENT.md](DEPLOYMENT.md)를 참고하십시오.

## 운영 기능

### Auto Sync

네이버 문의를 읽어 운영 DB에 반영합니다. `NAVER_AUTO_SYNC_ENABLED`와 주기를 환경변수로 관리하며, 동시 실행 lock과 실행 이력을 유지합니다.

### Auto Post와 Runtime Auto Post

`NAVER_POST_ENABLED`는 네이버 등록 기능의 환경 허용 Gate이고, `NAVER_AUTO_POST_ENABLED`는 자동 등록 Scheduler의 환경 Gate입니다. Dashboard의 Runtime Auto Post는 별도의 운영 스위치입니다. 세 조건과 검증 정책이 모두 충족되어야 자동 등록 경로가 동작합니다.

초기 서버 검증은 반드시 Runtime Auto Post `OFF / STOPPED` 상태에서 수행합니다. DPS가 필요한 문의에서 로그인 만료나 Agent 연결 실패가 발생하면 등록하지 않고 직원 확인 상태로 보류합니다.

### Learning

- **Historical Verified Learning**: 직원의 과거 문의·답변을 기본 검증 학습으로 사용하며 잘못된 사례만 제외합니다.
- **Positive Learning**: 검토 결과가 긍정적으로 확정된 답변을 학습합니다.
- **Copilot Correction Learning**: Copilot 답변을 직원이 교정한 결과를 더 강한 신호로 사용합니다.
- 개인정보 마스킹, quality score, policy risk, 시점 의존 사실과 현재 facts 우선 정책은 계속 적용됩니다.

### DPS Session Monitor와 Keepalive

Session Monitor는 `READY`, `LOGIN_REQUIRED`, `CHROME_NOT_FOUND`, `DPS_PAGE_NOT_FOUND`, `CONNECTION_FAILED`, `UNKNOWN`을 구분합니다. 로그인은 사용자가 Chrome에서 직접 수행합니다.

Keepalive는 검증된 DPS 탭의 새로고침 버튼만 사용하는 read-only 동작입니다. 실제 주문 조회와 같은 lock을 공유하며 조회가 우선합니다. DPS 서버의 absolute timeout은 Keepalive로 연장할 수 없습니다. 실환경 확인 전에는 `DPS_SESSION_KEEPALIVE_ENABLED=false`로 운영할 수 있습니다.

## 운영 주의사항

- DPS 자동화에는 로그인된 Windows 사용자와 활성 GUI desktop이 필요합니다.
- Windows Lock, 사용자 로그아웃, Chrome 종료, RDP 종료 방식은 `pywinauto` 동작에 영향을 줄 수 있습니다.
- Streamlit 포트를 인증과 HTTPS 없이 인터넷에 직접 공개하지 마십시오.
- 운영 DB·백업·로그·`.env`·DPS 상태 JSON을 GitHub에 올리지 마십시오.
- DB 이전 전후에는 SQLite `integrity_check`, `foreign_key_check`, Migration 및 주요 row count를 비교하십시오.
- 운영 전에는 Auto Sync, DPS, 답변 생성, Validator를 먼저 확인하고 Auto Post는 마지막에 별도 승인 후 활성화하십시오.

## 테스트

전체 테스트:

```powershell
python -m pytest -p no:cacheprovider -q
```

compileall:

```powershell
python -m compileall -q app.py config.py ai answer api core dps repositories scripts services tests uat ui workflow
```

Streamlit은 AppTest와 로컬 `/_stcore/health` 응답으로 기동 상태를 확인합니다. 테스트 중에는 네이버 POST, Auto Post, Auto Sync와 DPS Session Monitor 환경 Gate를 비활성화하십시오.
