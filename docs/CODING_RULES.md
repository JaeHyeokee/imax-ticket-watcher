# 개발 규칙 (Coding Rules)

> **이 문서는 코딩 규칙의 단일 출처(single source of truth)다.**
> 모든 작업은 이 문서를 전적으로 따른다.
>
> 규칙은 **사용자의 개별 지시보다 우선하는 프로젝트 상수**다.

이 프로젝트는 개인용 감시 스크립트(`watcher.py` 단일 모듈, 현재 약 380줄)다.
배포되는 라이브러리나 다중 모듈 서비스가 아니므로, 아래 규칙은 **지금 규모에 맞는
최소한의 일관성**을 목표로 한다. 프로젝트가 실제로 여러 파일로 쪼갤 만큼 커지기
전까지는 구조를 미리 만들지 않는다 (§D 참고).

---

## §A. 언어 & 스타일

- Python 3.9+ (표준 라이브러리 우선, 새 의존성 추가는 꼭 필요할 때만 — 현재 유일한
  서드파티 의존성은 `PyYAML`)
- **네이밍(PEP 8 기반)**
  - 함수·변수·모듈: `snake_case`
  - 모듈 레벨 상수: `ALL_CAPS` (예: `CANCEL_JITTER_RANGE`, `API_BASE`)
  - 클래스(도입 시): `PascalCase`
  - 호출처가 정확히 하나뿐인 예외적 헬퍼(예: 다른 함수가 스레드로 실행하려고 감싼
    내부 wrapper)에는 앞에 `_` 하나 (예: `send_telegram`이 백그라운드 스레드로 넘기는
    `_send_telegram_sync`). 이 프로젝트는 모듈이 하나뿐이라 대부분의 헬퍼 함수는
    여러 곳에서 재사용되는 모듈의 사실상 공개 표면이므로, `_`를 붙이지 않는 게
    기본값이다 — 모든 내부 헬퍼에 일괄로 `_`를 붙이지 않는다.
- **타입힌트**: 기존 코드 전체에 없으므로 강제하지 않는다. 새 함수에 추가하는 것은
  자유이나, 기존 함수들과 스타일이 크게 어긋나지 않게 부분적으로만 적용한다
  (전체 일괄 추가 같은 대규모 변경은 하지 않는다 — `REFACTORING_GUIDE.md` §10).
- **동시성 전제 문서화**: 스레드(`ThreadPoolExecutor`, `threading.Thread`)로 공유
  상태(`state` dict)를 건드리는 코드는 어떤 범위까지 GIL에 기대는지, 진짜 잠금이
  필요한 구간이 있는지를 주석으로 남긴다. 현재는 각 스레드가 서로 다른 키
  (`ymd` 단위)에만 쓰기 때문에 별도 lock 없이 동작한다는 전제를 유지한다 — 이
  전제를 깨는 변경(같은 키에 여러 스레드가 동시에 쓰기 등)을 하려면 lock 도입을
  함께 검토한다.

---

## §B. 네이밍·상수

1. **매직 넘버 금지.** 반복되거나 의미가 있는 상수는 모듈 상단에 `ALL_CAPS`
   상수로 뺀다. 지금 코드의 `CANCEL_JITTER_RANGE`, `FULL_SCAN_CHUNK_SIZE` 같은
   패턴을 그대로 따른다. 설정으로 노출할 값은 상수 대신 `config.get(key, 기본값)`
   형태로 `config.yaml`에서 오버라이드 가능하게 한다 (기존 패턴 유지).
2. **동작 분기는 명확한 이름의 헬퍼 함수로.** `matches_screen`, `is_red_day`처럼
   불리언을 반환하는 작은 함수로 조건을 명명한다. 문자열 비교 자체(`kw in name`)는
   이 프로젝트 성격상(외부 API 응답 필드 매칭) 자연스러운 방식이라 금지하지 않는다.
3. **파일명**: `snake_case.py`. 새 모듈이 생기면 책임 하나당 파일 하나
   (§D 성장 기준 참고).

---

## §C. 오류 처리

기존 코드가 이미 따르고 있는 2단 패턴을 유지한다.

1. **개별 요청/조회 함수는 실패를 예외로 올리지 않고 `None`/빈 값으로 삼킨다.**
   `fetch_json`이 재시도 후에도 실패하면 `None`을 반환하고, 호출자(`get_open_dates`,
   `get_sessions` 등)는 falsy 체크 후 빈 리스트로 대체한다. 이 계층에서 예외를
   그대로 전파하지 않는다.
2. **넓은 범위의 예외 처리는 "최상위 경계"에서만 한다.** 최상위 경계는 두 가지다:
   - 가장 바깥 폴링 루프(`main`의 `while True`) — `poll_once` 호출을 감싼
     `except Exception`이 감시 루프 자체가 죽는 것을 막는 최후 방어선.
   - **백그라운드 스레드의 target 함수** — `threading.Thread(target=...)`로 넘기는
     함수(예: `_send_telegram_sync`)는 그 자체가 해당 스레드의 최상위 경계다.
     스레드 target에서 예외가 안 잡히면 호출자에게 전파되지 않고 조용히 사라져서
     로그도 안 남기 때문에, 여기서도 `except Exception`으로 잡고 반드시 로그를
     남긴다.
   이 두 경계 밖에서 새로 넓은 `except Exception`을 추가하지 않는다 — 실패를
   조용히 삼키면 원인 추적이 어려워진다.
3. **예외/실패는 반드시 로그로 남긴다.** `log.warning`(재시도 가능한 실패),
   `log.error`(개별 작업 실패), `log.exception`(루프 최상단, 스택트레이스 포함)을
   상황에 맞게 구분해서 쓴다. 조용히 `pass`하지 않는다.

---

## §D. 프로젝트 구조

**지금은 flat 구조를 유지한다.**

```
watcher.py            # 전체 로직 (진입점 겸 유일한 모듈)
config.yaml            # 실제 설정 (git 추적 안 함)
config.example.yaml    # 설정 템플릿
state.json              # 런타임 상태 (git 추적 안 함)
requirements.txt
run.bat
docs/                   # CODING_RULES.md, REFACTORING_GUIDE.md, CHANGELOG.md
README.md
```

`include/`·`internal/`·`src/`·`test/` 같은 계층 분리는 라이브러리 배포를 전제로
한 구조라 이 프로젝트에는 적용하지 않는다.

**패키지 분리 기준 (성장 시에만 적용)** — 아래 조건 중 하나가 실제로 발생하기
전까지는 미리 쪼개지 않는다:

- `watcher.py`가 감당하기 어려울 정도로 길어짐 (대략 600줄 이상, 또는 한 파일 안에
  섞인 책임 때문에 특정 부분을 찾기 어려워질 때)
- CGV 외 다른 사이트/다른 알림 채널(디스코드 등)처럼 **서로 독립적으로 교체 가능한
  구현**이 추가됨

그 시점이 오면 책임 단위로 분리한다 (예시일 뿐, 필요할 때 실제 코드 보고 재판단):

```
src/imax_watcher/
  __main__.py     # main() 진입점, 폴링 루프
  api.py          # fetch_json, get_open_dates, get_sessions
  matching.py      # matches_screen/movie/time, is_red_day
  notify.py        # format_*, send_telegram
  state.py         # load_state/save_state
tests/
  test_matching.py  # 등, 순수 함수부터 우선 커버
```

---

## §E. 주석 작성 룰

> **원칙: 주석은 코드가 표현할 수 없는 것만 담는다.** 무엇을 하는지는 함수 이름이
> 말하고, 왜 그런지의 근거·제약만 주석이 담는다.

- **docstring은 강제하지 않는다.** 함수 이름과 매개변수만으로 동작이 명확하면
  (`matches_screen`, `session_key` 등) docstring 없이 둔다. `request_jitter`,
  `send_telegram`처럼 **동작의 이유가 이름만으로 안 드러나는 함수**에만 한 줄
  docstring을 단다.
- **"왜"를 남기는 인라인 주석은 적극 허용.** `CANCEL_JITTER_RANGE` 위 주석,
  `active_dates` 가지치기 이유 주석처럼 비자명한 설계 근거(왜 이 값인지, 왜 이
  순서인지)는 계속 남긴다. 반대로 코드가 이미 말하는 것("루프를 돈다")은 쓰지 않는다.
- **민감정보 금지.** 봇 토큰·chat_id 등은 어떤 로그·주석·커밋 메시지에도 값 자체를
  남기지 않는다.
- **이력 서술 금지.** "예전엔 이렇게 했다", 날짜, 변경 경위는 코드 주석이 아니라
  git 커밋 메시지 / `docs/CHANGELOG.md`(§F) 소관.
- **주석 처리된 코드(dead code) 금지.** 필요 없으면 삭제 (git 이력에 남는다).
- 한국어로 작성 (기존 코드 관례 유지).

---

## §F. 테스트

현재 이 프로젝트에는 자동화된 테스트가 없다. 강제로 테스트 스위트를 갖추라고
요구하지 않는다 — 개인 스크립트 규모에서 테스트 인프라를 먼저 만드는 것은
`REFACTORING_GUIDE.md` §10의 "과도한 리팩토링"에 해당한다.

다만 순수 함수(외부 I/O 없이 입력→출력만 있는 것: `matches_screen`,
`matches_movie`, `is_red_day`, `matches_time`, `session_key`, `format_datetime`
등)를 새로 추가하거나 수정할 때는 `pytest`로 간단한 단위 테스트를 같이 추가하는
것을 권장한다. 강제 사항은 아니다.

---

## §G. CHANGELOG 작성 룰

`docs/CHANGELOG.md`는 §E가 코드 주석에서 금지한 이력 서술(날짜·결정 경위·
"구 X 폐지/변경")의 공식 수용처다. [Keep a Changelog](https://keepachangelog.com/)
형식을 따르며, 카테고리는 **Added · Changed · Fixed · Removed** 4종을 쓴다.

정식 릴리즈 개념 없이 계속 굴러가는 개인 스크립트이므로, 버전 번호 대신 날짜
기준으로 최하단에 항목을 쌓는다.

```markdown
## [Unreleased]

### Added
- 새로운 기능

### Changed
- 기존 기능 변경

### Fixed
- 버그 수정

### Removed
- 제거된 기능
```
