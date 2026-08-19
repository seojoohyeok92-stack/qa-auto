# DPS 서버 운영 및 Learning 유효성

## 서버 PC DPS 전제조건

DPS 연동은 HTTP DPS API가 아니라 서버 PC의 일반 Chrome 창을 Windows UI Automation으로 조작한다. 서버에는 Chrome과 DPS 탭이 열려 있어야 하고 운영자가 직접 로그인/OTP를 완료해야 한다. 애플리케이션은 DPS ID, 비밀번호, 토큰 또는 쿠키를 저장하거나 로그에 기록하지 않는다.

`.env`는 Streamlit 실행 working directory가 아니라 프로젝트 루트의 `.env`에서 읽는다. `DPS_CONNECTION_FILE`과 `DPS_AGENT_STATE_FILE`이 상대 경로이면 프로젝트 루트를 기준으로 해석한다. 서버별 절대경로 하드코딩은 필요하지 않다.

필수 확인 항목:

- 서버가 Windows이고 대화형 desktop session을 사용할 수 있는지
- Chrome에서 실제 DPS 사내망/VPN/DNS/방화벽 접근이 되는지
- DPS 탭 제목과 로그인 상태가 Agent 진단 화면에서 감지되는지
- `DPS_AGENT_HOST`, `DPS_AGENT_PORT`가 유효한지
- Streamlit 프로세스 권한으로 `data/`, `logs/`에 쓸 수 있는지

네트워크·VPN·사내 인증서 문제는 애플리케이션이 우회하지 않는다. Dashboard의 DPS 진단 코드와 `logs/dps_agent.log`의 단계별 이벤트로 구분한다.

## DPS 시간축

- 데이터 갱신: 주문 조회는 demand-driven이며 성공 결과 freshness 기본값은 30분이다 (`DPS_REFRESH_INTERVAL_MINUTES=30`). 조회할 주문이 없으면 DPS 화면을 조작하지 않는다.
- 세션 검사: 기본 60초마다 가볍게 확인한다.
- 세션 유지: 마지막 성공한 실제 DPS interaction 이후 40분 idle일 때만 수행한다 (`DPS_SESSION_IDLE_MINUTES=40`).

세션 연장에 사용하는 정확한 동작은 DPS 화면의 `로그인시간연장` 컨트롤 클릭이다. 일반 페이지 refresh나 임의 URL/heartbeat 요청을 사용하지 않는다. 클릭 후 로그인된 DPS 화면이 `READY`로 다시 검증된 경우에만 `last_dps_activity_at`을 갱신한다. 성공한 주문조회도 activity를 갱신한다. Cache 확인, Streamlit 조작, 실패한 조회와 실패한 keep-alive는 갱신하지 않는다.

주문조회와 keep-alive는 같은 Agent의 lookup gate/lock을 공유하며 동시에 GUI를 조작하지 않는다. 세션 만료 시 자동으로 날짜를 추측하거나 무한 로그인하지 않고 `AUTH_ERROR`/`LOGIN_REQUIRED`로 운영자에게 알린다.

## 네이버 Auto Sync와 Dashboard 스위치

네이버 Auto Sync 기본 주기는 10분이다 (`NAVER_AUTO_SYNC_INTERVAL_MINUTES=10`). 프로세스 내 singleton registry와 SQLite leader/store lease를 사용하므로 Streamlit rerun이나 다중 프로세스가 중복 sync를 만들지 않는다.

Dashboard Auto Post OFF는 문의 수집을 중단하지 않는다. Auto Sync는 신규 문의를 DB에 저장하지만 durable event는 runtime 정책에 따라 자동답변/POST 진입을 차단한다. ON 전환 시 기존 OFF backlog를 무조건 일괄 POST하지 않도록 event 생성 시점의 runtime 상태가 보존된다.

## Learning 유효성

기존 Learning은 migration 후 `PERMANENT`로 유지된다. `TEMPORARY` Learning은 KST 기준 시작/종료 범위와 수동 활성 여부가 모두 충족될 때만 공통 Learning candidate repository를 통과한다. 시작 전·만료·수동 비활성 Learning은 DB와 Learning Manager 이력에는 남지만 GPT/Fake answer context에는 전달되지 않는다.
