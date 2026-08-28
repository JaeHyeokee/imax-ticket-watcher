import argparse
import json
import random
import threading
import time
import logging
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "state.json"

API_BASE = "https://cgv.co.kr/api/v1/booking"
CO_CD = "A420"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://cgv.co.kr/cnm/movieBook/cinema",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("imax-watcher")


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            "config.yaml이 없습니다. config.example.yaml을 config.yaml로 복사한 뒤 "
            "텔레그램 봇 토큰과 chat_id를 채워주세요."
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw_state = json.load(f)
            raw_state["seen"] = set(raw_state.get("seen", []))
            raw_state.setdefault("seat_counts", {})
            raw_state.setdefault("active_dates", {})
            raw_state.setdefault("full_scan_cursor", {})
            return raw_state
    return {
        "seen": set(),
        "open_dates": {},
        "seat_counts": {},
        "active_dates": {},
        "full_scan_cursor": {},
        "initialized": False,
    }


def save_state(state):
    serializable = {
        "seen": sorted(state["seen"]),
        "open_dates": state["open_dates"],
        "seat_counts": state["seat_counts"],
        "active_dates": state["active_dates"],
        "full_scan_cursor": state["full_scan_cursor"],
        "initialized": state["initialized"],
    }
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


# 연속 요청이 한 순간에 겹치지 않도록 약간의 지연을 둠 (서버 부하 완화 목적).
# 취소표 감시(added/active_dates)는 반응 속도가 중요해서 폭을 좁게, 전체 오픈 재검사는 덜 급하니 폭을 넓게 둠.
CANCEL_JITTER_RANGE = (0.4, 1.2)
FULL_SCAN_JITTER_RANGE = (0.6, 2.5)

# 폴링 루프 대기 시간에도 약간의 편차를 둬서 매 사이클 요청이 정확히 같은 순간에 뭉치지 않게 함.
CANCEL_INTERVAL_JITTER_RATIO = 0.15
FULL_SCAN_INTERVAL_JITTER_RATIO = 0.35

# 취소표 재확인(active_dates)은 감시 중인 날짜 수만큼 순차 조회하면 한 바퀴가 너무 길어지므로
# 소수의 동시 요청으로 병렬 처리. 너무 늘리면 순간적으로 요청이 몰려 비정상 접속으로 보일 위험이 커지니
# 작은 값으로 제한.
CANCEL_MAX_WORKERS = 4

# fetch_json이 재시도를 다 소진하고도 실패한 횟수(성공하면 0으로 리셋)가 이 값 이상
# 연속되면 CGV 서버 장애나 접속 차단 가능성으로 보고 폴링 주기를 늘리고 텔레그램으로
# 알림. 극장 하나 조회가 일시적으로 흔들리는 정도로는 잘 안 넘도록 충분히 높게 잡음.
CONSECUTIVE_FAILURE_ALERT_THRESHOLD = 10
FAILURE_BACKOFF_MULTIPLIER = 4  # 실패가 계속되는 동안 폴링 주기를 이 배수만큼 늘림
FAILURE_BACKOFF_MAX_SEC = 300  # 백오프 상한 (5분) — 무한정 늘어나지 않게 캡

_consecutive_failures_lock = threading.Lock()
_consecutive_failures = 0


def request_jitter(jitter_range=CANCEL_JITTER_RANGE):
    """연속 요청 사이에 랜덤 딜레이를 줘서 짧은 시간에 요청이 몰리는 것을 완화."""
    time.sleep(random.uniform(*jitter_range))


def _record_fetch_result(success):
    """fetch_json 성공/실패를 전역 연속 실패 카운터에 반영하고 갱신된 값을 반환.
    parallel=True인 취소표 재확인에서 여러 스레드가 동시에 호출할 수 있어 lock으로 보호."""
    global _consecutive_failures
    with _consecutive_failures_lock:
        _consecutive_failures = 0 if success else _consecutive_failures + 1
        return _consecutive_failures


def get_consecutive_failures():
    return _consecutive_failures


def fetch_json(url, retries=3, timeout=10):
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            _record_fetch_result(success=True)
            return result
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            # ValueError는 json.JSONDecodeError/UnicodeDecodeError 포함 — 차단 시 CGV가
            # JSON 대신 HTML 안내 페이지 등을 돌려주는 경우도 재시도 대상으로 잡기 위함.
            log.warning(f"요청 실패 ({attempt}/{retries}): {url} - {e}")
            time.sleep(2 * attempt)
    _record_fetch_result(success=False)
    return None


def get_open_dates(site_no):
    url = f"{API_BASE}/searchSiteScnscYmdListBySite?coCd={CO_CD}&siteNo={site_no}"
    response = fetch_json(url)
    if not response or response.get("statusCode") != 0:
        return []
    return [row["scnYmd"] for row in response.get("data") or []]


def get_sessions(site_no, scn_ymd):
    url = f"{API_BASE}/searchMovScnInfo?coCd={CO_CD}&siteNo={site_no}&scnYmd={scn_ymd}&rtctlScopCd=08"
    response = fetch_json(url)
    if not response or response.get("statusCode") != 0:
        return []
    return response.get("data") or []


def matches_screen(session, screen_keywords):
    if not screen_keywords:
        return True
    name = session.get("scnsNm", "")
    return any(kw in name for kw in screen_keywords)


def matches_movie(session, movie_keywords):
    if not movie_keywords:
        return True
    names = [session.get("movNm", ""), session.get("engProdNm", ""), session.get("expoProdNm", "")]
    return any(kw in n for kw in movie_keywords for n in names)


def is_red_day(ymd, holidays):
    if ymd in holidays:
        return True
    if len(ymd) == 8:
        return datetime.strptime(ymd, "%Y%m%d").weekday() >= 5  # 토(5)/일(6)
    return False


def matches_time(session, red_day, weekday_start_time):
    if red_day or not weekday_start_time:
        return True
    start = session.get("scnsrtTm", "")
    return len(start) == 4 and start >= weekday_start_time


def session_key(session):
    return "|".join([
        session.get("siteNo", ""),
        session.get("scnYmd", ""),
        session.get("scnsNo", ""),
        session.get("scnsrtTm", ""),
        session.get("movNo", ""),
    ])


WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def format_datetime(session):
    ymd = session.get("scnYmd", "")
    if len(ymd) == 8:
        date_fmt = f"{ymd[0:4]}.{ymd[4:6]}.{ymd[6:8]}"
        weekday = WEEKDAY_KR[datetime.strptime(ymd, "%Y%m%d").weekday()]
        date_fmt = f"{date_fmt}({weekday})"
    else:
        date_fmt = ymd
    start = session.get("scnsrtTm", "")
    time_fmt = f"{start[0:2]}:{start[2:4]}" if len(start) == 4 else start
    return date_fmt, time_fmt


def format_notification(theater_name, session):
    date_fmt, time_fmt = format_datetime(session)
    movie = session.get("movNm", "")
    screen = session.get("scnsNm", "")
    total = session.get("stcnt", "?")
    free = session.get("frSeatCnt", "?")
    return (
        f"🎬 예매 오픈 감지!\n"
        f"극장: {theater_name}\n"
        f"영화: {movie}\n"
        f"상영관: {screen}\n"
        f"일시: {date_fmt} {time_fmt}\n"
        f"좌석: {free} / {total}"
    )


def format_cancel_notification(theater_name, session, prev_free, new_free):
    date_fmt, time_fmt = format_datetime(session)
    movie = session.get("movNm", "")
    screen = session.get("scnsNm", "")
    freed = new_free - prev_free
    return (
        f"🎟 취소표 발생!\n"
        f"극장: {theater_name}\n"
        f"영화: {movie}\n"
        f"상영관: {screen}\n"
        f"일시: {date_fmt} {time_fmt}\n"
        f"잔여좌석: {new_free} / {total} (+{freed}석)"
    )


def send_telegram(config, text):
    """텔레그램 응답을 기다리지 않고 별도 스레드에서 비동기로 전송 (감시 루프가 멈추지 않도록)."""
    threading.Thread(target=_send_telegram_sync, args=(config, text), daemon=True).start()


def _send_telegram_sync(config, text):
    token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        log.info("텔레그램 알림 전송 완료")
    except Exception as e:
        log.error(f"텔레그램 알림 전송 실패: {e}")


def scan_dates(config, state, theater, dates, notify_open, notify_cancel, jitter_range=CANCEL_JITTER_RANGE, parallel=False):
    site_no = theater["site_no"]
    name = theater["name"]
    screen_keywords = config.get("screens", ["IMAX"])
    movie_keywords = config.get("movies", [])
    min_alert_seats = theater.get("min_alert_seats", 0)
    weekday_start_time = config.get("weekday_start_time", "")
    holidays = set(config.get("holidays", []))
    active = set(state["active_dates"].get(site_no, []))
    changed = False

    # parallel=True일 때 ThreadPoolExecutor로 여러 스레드가 이 클로저를 동시에 실행하며
    # state["seen"]/state["seat_counts"]/active/changed를 건드림. 각 스레드는 서로 다른
    # ymd의 세션(따라서 서로 다른 session_key)에만 쓰기 때문에 lock 없이 안전함. changed는
    # 여러 스레드가 동시에 건드려도 항상 같은 값(True)만 대입하는 멱등 쓰기라 마찬가지로
    # 안전함. 같은 키를 여러 스레드가 동시에 쓰게 되는 변경을 하려면 lock 도입을 함께
    # 검토해야 함.
    def process_date(ymd):
        nonlocal changed
        red_day = is_red_day(ymd, holidays)
        sessions = get_sessions(site_no, ymd)
        request_jitter(jitter_range)
        date_has_match = False
        for session in sessions:
            if not matches_screen(session, screen_keywords):
                continue
            if not matches_movie(session, movie_keywords):
                continue
            if not matches_time(session, red_day, weekday_start_time):
                continue
            date_has_match = True
            key = session_key(session)
            free = int(session.get("frSeatCnt") or 0)
            prev_seat_count = state["seat_counts"].get(key)

            if key not in state["seen"]:
                state["seen"].add(key)
                if notify_open and free > min_alert_seats:
                    text = format_notification(name, session)
                    date_fmt, time_fmt = format_datetime(session)
                    log.info(f"새 상영 발견 -> {name} {session.get('movNm')} {date_fmt} {time_fmt}")
                    send_telegram(config, text)
            else:
                prev = prev_seat_count if prev_seat_count is not None else free
                if free > prev:
                    if notify_cancel and free > min_alert_seats:
                        text = format_cancel_notification(name, session, prev, free)
                        date_fmt, time_fmt = format_datetime(session)
                        log.info(
                            f"취소표 발생 -> {name} {session.get('movNm')} {date_fmt} "
                            f"{time_fmt} ({prev} -> {free})"
                        )
                        send_telegram(config, text)

            if prev_seat_count != free:
                state["seat_counts"][key] = free
                changed = True

        if date_has_match and ymd not in active:
            active.add(ymd)
            changed = True

    if parallel and len(dates) > 1:
        max_workers = min(config.get("cancel_max_workers", CANCEL_MAX_WORKERS), len(dates))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            list(executor.map(process_date, dates))
    else:
        for ymd in dates:
            process_date(ymd)

    state["active_dates"][site_no] = sorted(active)
    return changed


def initialize(config, state):
    log.info("최초 실행: 현재 상영정보를 기준선으로 저장합니다 (알림 없음)")
    for theater in config["theaters"]:
        site_no = theater["site_no"]
        dates = get_open_dates(site_no)
        state["open_dates"][site_no] = dates
        scan_dates(config, state, theater, dates, notify_open=False, notify_cancel=False)
    state["initialized"] = True
    save_state(state)
    log.info("초기화 완료. 이제부터 새로운 상영이 열리면 알림을 보냅니다.")


def prune_expired_sessions(state, site_no, open_dates_set):
    """마감되어 더 이상 예매 가능 목록에 없는 날짜의 seen/seat_counts 항목을 제거.
    active_dates 가지치기와 같은 목적 — 오래 켜둘수록 state.json이 무한정 커지는 것을 방지."""
    prefix = f"{site_no}|"
    expired_keys = [
        key for key in state["seen"]
        if key.startswith(prefix) and key.split("|")[1] not in open_dates_set
    ]
    for key in expired_keys:
        state["seen"].discard(key)
        state["seat_counts"].pop(key, None)
    return expired_keys


def poll_once(config, state, do_full_scan, watch_open=True, watch_cancel=True):
    state_changed = False

    for theater in config["theaters"]:
        site_no = theater["site_no"]
        prev_dates = set(state["open_dates"].get(site_no, []))
        new_dates = get_open_dates(site_no)
        request_jitter()
        added = [d for d in new_dates if d not in prev_dates]
        new_dates_set = set(new_dates)
        state["open_dates"][site_no] = new_dates
        if new_dates_set != prev_dates:
            state_changed = True

        # active_dates 가지치기: 더 이상 예매 가능 목록에 없는(상영 종료/마감된) 날짜는 감시 대상에서 제거.
        # 그래야 취소표 감시가 시간이 지날수록 요청량이 계속 늘어나지 않음. watch_cancel이 꺼져 있어도
        # active_dates 목록 자체는 계속 최신 상태로 유지해야 나중에 다시 켰을 때 정상 동작하므로 항상 수행.
        prev_active = state["active_dates"].get(site_no, [])
        pruned_active = [d for d in prev_active if d in new_dates_set]
        expired = [d for d in prev_active if d not in new_dates_set]
        if expired:
            log.info(f"{theater['name']}: 마감/종료된 날짜 감시 해제 {expired}")
            state_changed = True
        state["active_dates"][site_no] = pruned_active

        # seen/seat_counts 가지치기: 위 active_dates 가지치기와 같은 이유로, 마감된 날짜의
        # 감시 기록도 함께 제거해야 state.json이 계속 커지는 것을 막을 수 있음.
        expired_sessions = prune_expired_sessions(state, site_no, new_dates_set)
        if expired_sessions:
            log.info(f"{theater['name']}: 마감된 날짜의 감시 기록 {len(expired_sessions)}건 정리")
            state_changed = True

        if watch_open and added:
            log.info(f"{theater['name']}: 예매 가능 날짜 확장 감지 {added}")
            if scan_dates(config, state, theater, added, notify_open=watch_open, notify_cancel=watch_cancel):
                state_changed = True

        # 취소표 감시: 이미 감시 대상으로 확인된 날짜만 빠르게 재확인 (요청 적음 -> 짧은 주기로 실행)
        # 날짜 수가 많아지면 순차 조회로는 한 바퀴가 길어지므로 소수의 동시 요청으로 병렬 처리.
        active_dates = state["active_dates"].get(site_no, [])
        if watch_cancel and active_dates:
            if scan_dates(
                config, state, theater, active_dates,
                notify_open=watch_open, notify_cancel=watch_cancel, parallel=True,
            ):
                state_changed = True

        # 오픈 감시: 예매 가능한 전체 기간을 다시 훑어서 놓친 신규 상영이 없는지 확인
        # 날짜를 한 번에 다 훑지 않고, 매번 일부(chunk)씩만 순환하며 훑어서 한 사이클이 길어지는 것을 방지
        if watch_open and do_full_scan and new_dates:
            chunk_size = config.get("full_scan_chunk_size", 10)
            cursor = state["full_scan_cursor"].get(site_no, 0) % len(new_dates)
            chunk = (new_dates[cursor:] + new_dates[:cursor])[:chunk_size]
            if scan_dates(
                config, state, theater, chunk,
                notify_open=watch_open, notify_cancel=watch_cancel, jitter_range=FULL_SCAN_JITTER_RANGE,
            ):
                state_changed = True
            state["full_scan_cursor"][site_no] = (cursor + len(chunk)) % len(new_dates)
            state_changed = True

    # 아무것도 안 바뀐 사이클(대부분)에는 state.json을 다시 쓰지 않아 불필요한 디스크 I/O를 피함.
    if state_changed:
        save_state(state)


def parse_args():
    parser = argparse.ArgumentParser(description="IMAX 예매 오픈/취소표 감시 봇")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--only-open", action="store_true", help="신규 상영(예매 오픈) 감지만 실행 (취소표 감시 끔)"
    )
    mode.add_argument(
        "--only-cancel", action="store_true", help="취소표(잔여좌석 증가) 감지만 실행 (오픈 감시 끔)"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    watch_open = not args.only_cancel
    watch_cancel = not args.only_open

    config = load_config()
    state = load_state()

    if not state["initialized"]:
        initialize(config, state)

    cancel_check_interval = config.get("cancel_check_interval_sec", 8)
    full_scan_interval = config.get("full_scan_interval_sec", 30)
    full_scan_chunk_size = config.get("full_scan_chunk_size", 10)
    if watch_open and watch_cancel:
        mode_desc = "오픈+취소표 감지"
    elif watch_open:
        mode_desc = "오픈 감지만"
    else:
        mode_desc = "취소표 감지만"

    # cancel_check_interval은 취소표 감시뿐 아니라 폴링 루프 자체의 주기이기도 해서
    # (added dates 확인이 매 사이클 돈다) watch_cancel이 꺼져 있어도 의미는 있지만,
    # 그 경우 "취소표 확인 주기"라는 라벨은 오해를 주므로 "폴링 주기"로 바꿔 표기.
    poll_label = "취소표 확인 주기" if watch_cancel else "폴링 주기"
    full_scan_desc = (
        f", 전체 오픈 재검사 주기: {full_scan_interval}s, 회당 {full_scan_chunk_size}일씩 순환 조회"
        if watch_open
        else ""
    )
    log.info(f"감시 시작 ({mode_desc}, {poll_label}: {cancel_check_interval}s{full_scan_desc})")

    last_full_scan = None  # None -> 시작 직후 1회는 무조건 첫 청크 조회
    next_full_scan_interval = full_scan_interval  # 매번 지터를 넣어 재계산되는 실제 대기 시간
    alerted_failure = False  # 연속 실패 알림을 한 번만 보내고, 복구 알림도 한 번만 보내기 위한 엣지 트리거

    while True:
        now = time.monotonic()
        do_full_scan = watch_open and (
            last_full_scan is None or (now - last_full_scan) >= next_full_scan_interval
        )
        try:
            poll_once(config, state, do_full_scan, watch_open=watch_open, watch_cancel=watch_cancel)
            if do_full_scan:
                last_full_scan = now
                next_full_scan_interval = full_scan_interval * random.uniform(
                    1 - FULL_SCAN_INTERVAL_JITTER_RATIO, 1 + FULL_SCAN_INTERVAL_JITTER_RATIO
                )
        except Exception as e:
            log.exception(f"폴링 중 오류 발생: {e}")

        # 연속 실패가 임계치를 넘으면 CGV 서버 장애/접속 차단 가능성으로 보고 폴링 주기를
        # 늘려서 계속 두드리지 않게 하고, 텔레그램으로 한 번만 알림(복구되면 복구 알림도 한 번).
        failures = get_consecutive_failures()
        if failures >= CONSECUTIVE_FAILURE_ALERT_THRESHOLD:
            if not alerted_failure:
                log.error(f"연속 {failures}회 요청 실패 - CGV 서버 장애 또는 접속 차단 가능성")
                send_telegram(
                    config,
                    f"⚠️ 감시가 연속 {failures}회 실패하고 있습니다. CGV 접속이 일시적으로 "
                    f"막혔거나 서버에 문제가 있을 수 있어요. 폴링 주기를 늘려서 계속 재시도합니다.",
                )
                alerted_failure = True
            base_interval = min(cancel_check_interval * FAILURE_BACKOFF_MULTIPLIER, FAILURE_BACKOFF_MAX_SEC)
        else:
            if alerted_failure:
                log.info("요청이 다시 정상적으로 성공했습니다.")
                send_telegram(config, "✅ 감시가 다시 정상적으로 동작합니다.")
                alerted_failure = False
            base_interval = cancel_check_interval

        sleep_for = base_interval * random.uniform(
            1 - CANCEL_INTERVAL_JITTER_RATIO, 1 + CANCEL_INTERVAL_JITTER_RATIO
        )
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
