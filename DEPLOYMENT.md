# Q&A Auto 서버 배포

이 문서는 Windows 서버 PC에서 Q&A Auto를 설치하고 운영 검증하는 절차입니다. 운영 DB, `.env`, Secret, 로그와 DPS 로컬 상태는 GitHub 저장소와 분리합니다.

## 서버 설치 순서

1. Windows용 Python을 설치하고 `python --version`으로 확인합니다.
2. Git을 설치하고 `git --version`으로 확인합니다.
3. 사용자가 생성한 Private GitHub Repository를 clone합니다.
4. 프로젝트 폴더에서 virtual environment를 생성합니다.
5. `requirements.txt`를 설치합니다.
6. 별도로 전달한 운영 DB를 서버의 보호된 경로에 배치합니다.
7. `.env.example`을 복사해 `.env`를 만들고 서버에서 직접 Secret을 입력합니다.
8. Q&A Auto를 실행할 Windows 사용자로 로그인하여 Chrome을 실행합니다.
9. 해당 Chrome Profile에서 DPS에 수동 로그인합니다.
10. DPS 탭을 연 상태로 DPS Agent를 실행합니다.
11. 별도 프로세스로 Streamlit을 실행합니다.
12. Dashboard에서 DPS Session 상태가 `READY`인지 확인합니다.
13. Auto Sync를 수동·읽기 중심으로 확인하고 실행 이력을 점검합니다.
14. Runtime Auto Post가 `OFF / STOPPED`인지 확인한 상태로 답변 생성까지 테스트합니다.
15. Keepalive를 실환경에서 read-only로 확인합니다. 검증 전에는 OFF로 시작할 수 있습니다.
16. 사내망의 허용된 클라이언트에서 접속을 테스트합니다.
17. 다중 사용자의 조회·선택·DPS lock 동작을 테스트합니다.
18. 외부 공개 전 HTTPS, 인증, 권한, 방화벽과 reverse proxy/VPN을 구성합니다.
19. 모든 안전 검증과 운영 승인이 완료된 마지막 단계에서만 Runtime Auto Post를 ON으로 전환합니다.

## Clone 및 Python 환경

아래 경로와 Repository URL은 예시입니다.

```powershell
Set-Location -LiteralPath "C:\path\to"
git clone <PRIVATE_REPOSITORY_URL> qa-auto
Set-Location -LiteralPath "C:\path\to\qa-auto"

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

테스트와 유지보수 스크립트가 필요한 관리 환경에서는 다음도 설치합니다.

```powershell
python -m pip install -r requirements-dev.txt
```

## 운영 DB 이전

운영 DB는 GitHub에 포함하지 않습니다. 암호화되거나 접근 통제된 별도 전달 수단을 사용하십시오.

운영 DB에는 다음 데이터가 함께 들어 있습니다.

- 문의, 답변 초안, Final Answer, workflow와 activity log
- Learning Example과 Learning provenance
- Historical Case, 버전, Verified Learning/제외 상태
- Positive Learning과 Copilot Correction Learning
- Project Knowledge
- GPT Copilot session/message
- Auto Sync, Auto Post, Scheduler와 Migration 상태

기본 경로를 사용할 경우 clone 후 `data/oje_automation.db`에 배치합니다. 별도 보호 경로를 사용할 경우 `.env`의 `OJE_AUTOMATION_DB_PATH`에 지정합니다.

이전 전후에 DB 파일 크기·해시, Migration, 주요 table row count를 대조하고 다음을 확인합니다.

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

DB 파일과 함께 백업·WAL·SHM 파일을 Git에 추가하지 마십시오. 실행 중인 SQLite DB를 단순 복사하지 말고 안전한 SQLite backup 절차를 사용하십시오.

## `.env` 준비

`.env`는 clone되지 않으며 서버 PC에서 별도로 생성합니다.

```powershell
Copy-Item .env.example .env
```

Secret 값은 문서, 명령 이력, Git commit 또는 채팅에 남기지 마십시오. 조직에서 승인한 Secret 관리 방식을 권장합니다.

초기 안전값:

```dotenv
NAVER_POST_ENABLED=false
NAVER_AUTO_POST_ENABLED=false
DPS_SESSION_MONITOR_ENABLED=true
DPS_SESSION_KEEPALIVE_ENABLED=false
```

Naver Client ID/Secret과 GPT API key는 실제 기능을 승인받은 경우에만 서버 `.env` 또는 승인된 Secret store에 입력합니다.

## DPS 운영 조건

현재 DPS Agent는 Windows Chrome GUI와 `pywinauto`를 사용합니다.

- Windows 사용자가 로그인되어 있어야 합니다.
- 해당 사용자 세션에서 Chrome이 실행되어야 합니다.
- DPS 로그인은 사용자가 직접 수행합니다. 자동 로그인은 제공하지 않습니다.
- 로그인된 Chrome Profile과 DPS 탭을 Agent가 탐색합니다.
- 별도 `user-data-dir`을 강제하지 않습니다.
- Windows Lock, 사용자 로그아웃, 모니터/desktop 상태와 RDP 종료 방식이 UI Automation에 영향을 줄 수 있습니다.
- Chrome 또는 PC 재시작 후 DPS 세션 유지 여부는 Chrome cookie와 DPS 서버 timeout 정책에 따라 달라집니다.

권장 시작 순서:

```text
Windows 로그인
→ Chrome 실행
→ DPS 수동 로그인
→ DPS 탭 확인
→ DPS Agent 시작
→ Q&A Auto 시작
→ Dashboard READY 확인
```

Agent 실행:

```powershell
Set-Location -LiteralPath "C:\path\to\qa-auto"
.\.venv\Scripts\Activate.ps1
python -m dps.agent_server
```

Agent 기본 연결은 `127.0.0.1:8765`입니다. DPS Agent를 외부 네트워크에 직접 공개하지 마십시오.

Keepalive는 검증된 DPS 탭의 Chrome 새로고침 버튼을 사용하는 read-only 동작입니다. idle timeout 갱신 여부는 DPS 서버 정책에 따라 달라지며 absolute timeout은 연장할 수 없습니다. 실제 DPS 탭에서 안전성을 확인하기 전에는 OFF로 유지할 수 있습니다.

## Streamlit 실행

로컬 서버 PC에서만 먼저 확인합니다.

```powershell
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8502
```

사내망에서 접근해야 하는 경우 보안 구성을 완료한 후 다음과 같이 수신 주소를 변경할 수 있습니다.

```powershell
python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8502
```

Health 확인:

```text
http://127.0.0.1:8502/_stcore/health
```

## 운영 검증 체크리스트

- Dashboard와 문의 Pagination이 정상인가
- 운영 DB Migration과 row count가 이전 전과 같은가
- Runtime Auto Post가 `OFF / STOPPED`인가
- Chrome/DPS 탭 로그인 후 Session Monitor가 `READY`인가
- 로그인 만료 시 `LOGIN_REQUIRED`로 분리되는가
- DPS required 문의가 장애 시 직원 검토로 안전 보류되는가
- DPS 불필요 문의는 기존 흐름을 유지하는가
- Keepalive와 실제 DPS 조회가 동시에 Chrome을 조작하지 않는가
- Auto Sync가 Naver POST 없이 문의만 동기화하는가
- 전체 pytest, AppTest, compileall과 Streamlit health가 통과하는가
- 로그에 Secret, cookie, token 또는 고객 상세정보가 기록되지 않는가

## 외부 공개 보안

Streamlit 포트 8502를 인터넷에 직접 공개하는 것은 권장하지 않습니다. 최소한 다음을 구성한 뒤 외부 접근을 허용하십시오.

- HTTPS termination
- 사용자 인증과 역할 기반 권한
- 방화벽 allowlist
- Reverse proxy
- 사내 VPN 또는 secure tunnel
- 접근 로그와 보안 모니터링
- 운영 DB·`.env` 파일 권한 제한과 백업 암호화

GitHub Repository는 Private을 권장합니다. GitHub 인증은 운영자가 직접 수행하며 비밀번호나 Personal Access Token을 프로젝트 파일에 저장하지 않습니다.

## Server GUI coexistence / DPS Low Priority

DPS Windows Agent는 같은 서버의 Kakao, Naver, Ecount 등 다른 GUI
automation보다 낮은 우선순위로 동작합니다. 실제 foreground/UIA 조작 전에
현재 foreground 창과 최근 activity marker를 확인하고, 다른 GUI 작업이
감지되면 대기합니다. 신호가 사라진 뒤 cooldown을 거쳐 자동 재개합니다.
상주 dispatcher 또는 scheduler 프로세스의 존재만으로 BUSY 판정하지 않으므로
기존 서버 프로그램을 수정할 필요가 없습니다.

```dotenv
DPS_GUI_GUARD_ENABLED=true
DPS_GUI_GUARD_RECHECK_SECONDS=5
DPS_GUI_GUARD_COOLDOWN_SECONDS=5
DPS_GUI_RESOURCE_MAX_WAIT_SECONDS=600
DPS_GUI_GUARD_ACTIVITY_GRACE_SECONDS=15
DPS_GUI_GUARD_PROCESS_PATTERNS=
DPS_GUI_GUARD_WINDOW_PATTERNS=KakaoTalk,카카오톡
DPS_GUI_GUARD_ACTIVITY_PATHS=
```

`PROCESS_PATTERNS`는 전체 프로세스 목록이 아니라 현재 foreground 창의 소유
프로세스에만 적용됩니다. `ACTIVITY_PATHS`는 쉼표로 구분하며 상대경로는
qa-auto 기준입니다. 운영 `.env`의 값을 표시하지 않고 누락 key만 점검하거나
안전한 non-secret 기본값만 추가하려면 다음을 실행합니다.

```powershell
python scripts/check_env.py
python scripts/check_env.py --add-safe-defaults
```

기존 값은 덮어쓰지 않으며 secret과 빈 기본값은 자동 추가하지 않습니다.
`DPS_SESSION_KEEPALIVE_ENABLED=false`는 실제 서버 검증 전까지 유지하는 것을
권장합니다. Keepalive는 Guard가 BUSY이면 GUI를 빼앗지 않고 defer됩니다.
