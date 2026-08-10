import json
import random
import time
import logging
import urllib.request
import urllib.error
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
            data = json.load(f)
            data["seen"] = set(data.get("seen", []))
            data.setdefault("seat_counts", {})
            data.setdefault("active_dates", {})
            return data
    return {"seen": set(), "open_dates": {}, "seat_counts": {}, "active_dates": {}, "initialized": False}


def save_state(state):
    serializable = {
        "seen": sorted(state["seen"]),
        "open_dates": state["open_dates"],
        "seat_counts": state["seat_counts"],
        "active_dates": state["active_dates"],
        "initialized": state["initialized"],
    }
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def request_jitter():
    """연속 요청 사이에 랜덤 딜레이를 줘서 짧은 시간에 요청이 몰리는 것을 완화."""
    time.sleep(random.uniform(0.4, 1.0))


def fetch_json(url, retries=3, timeout=10):
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            log.warning(f"요청 실패 ({attempt}/{retries}): {url} - {e}")
            time.sleep(2 * attempt)
    return None


def get_open_dates(site_no):
    url = f"{API_BASE}/searchSiteScnscYmdListBySite?coCd={CO_CD}&siteNo={site_no}"
    data = fetch_json(url)
    if not data or data.get("statusCode") != 0:
        return []
    return [row["scnYmd"] for row in data.get("data") or []]


def get_sessions(site_no, scn_ymd):
    url = f"{API_BASE}/searchMovScnInfo?coCd={CO_CD}&siteNo={site_no}&scnYmd={scn_ymd}&rtctlScopCd=08"
    data = fetch_json(url)
    if not data or data.get("statusCode") != 0:
        return []
    return data.get("data") or []


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
    total = session.get("stcnt", "?")
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


def scan_dates(config, state, theater, dates, notify):
    site_no = theater["site_no"]
    name = theater["name"]
    screen_keywords = config.get("screens", ["IMAX"])
    movie_keywords = config.get("movies", [])
    min_alert_seats = theater.get("min_alert_seats", 0)
    weekday_start_time = config.get("weekday_start_time", "")
    holidays = set(config.get("holidays", []))
    active = set(state["active_dates"].get(site_no, []))

    for ymd in dates:
        red_day = is_red_day(ymd, holidays)
        sessions = get_sessions(site_no, ymd)
        request_jitter()
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

            if key not in state["seen"]:
                state["seen"].add(key)
                if notify and free > min_alert_seats:
                    text = format_notification(name, session)
                    date_fmt, time_fmt = format_datetime(session)
                    log.info(f"새 상영 발견 -> {name} {session.get('movNm')} {date_fmt} {time_fmt}")
                    send_telegram(config, text)
            else:
                prev = state["seat_counts"].get(key, free)
                if free > prev:
                    if notify and free > min_alert_seats:
                        text = format_cancel_notification(name, session, prev, free)
                        date_fmt, time_fmt = format_datetime(session)
                        log.info(
                            f"취소표 발생 -> {name} {session.get('movNm')} {date_fmt} "
                            f"{time_fmt} ({prev} -> {free})"
                        )
                        send_telegram(config, text)

            state["seat_counts"][key] = free

        if date_has_match:
            active.add(ymd)

    state["active_dates"][site_no] = sorted(active)


def initialize(config, state):
    log.info("최초 실행: 현재 상영정보를 기준선으로 저장합니다 (알림 없음)")
    for theater in config["theaters"]:
        site_no = theater["site_no"]
        dates = get_open_dates(site_no)
        state["open_dates"][site_no] = dates
        scan_dates(config, state, theater, dates, notify=False)
    state["initialized"] = True
    save_state(state)
    log.info("초기화 완료. 이제부터 새로운 상영이 열리면 알림을 보냅니다.")


def poll_once(config, state, do_full_scan):
    for theater in config["theaters"]:
        site_no = theater["site_no"]
        prev_dates = set(state["open_dates"].get(site_no, []))
        new_dates = get_open_dates(site_no)
        request_jitter()
        added = [d for d in new_dates if d not in prev_dates]
        state["open_dates"][site_no] = new_dates

        if added:
            log.info(f"{theater['name']}: 예매 가능 날짜 확장 감지 {added}")
            scan_dates(config, state, theater, added, notify=True)

        # 취소표 감시: 이미 감시 대상으로 확인된 날짜만 빠르게 재확인 (요청 적음 -> 짧은 주기로 실행)
        active_dates = state["active_dates"].get(site_no, [])
        if active_dates:
            scan_dates(config, state, theater, active_dates, notify=True)

        # 오픈 감시: 예매 가능한 전체 기간을 다시 훑어서 놓친 신규 상영이 없는지 확인 (요청 많음 -> 긴 주기로 실행)
        if do_full_scan:
            scan_dates(config, state, theater, new_dates, notify=True)

    save_state(state)


def main():
    config = load_config()
    state = load_state()

    if not state["initialized"]:
        initialize(config, state)

    cancel_check_interval = config.get("cancel_check_interval_sec", 8)
    full_scan_interval = config.get("full_scan_interval_sec", 300)
    log.info(
        f"감시 시작 (취소표 확인 주기: {cancel_check_interval}s, "
        f"전체 오픈 재검사 주기: {full_scan_interval}s)"
    )

    last_full_scan = None  # None -> 시작 직후 1회는 무조건 전체 재검사

    while True:
        now = time.monotonic()
        do_full_scan = last_full_scan is None or (now - last_full_scan) >= full_scan_interval
        try:
            poll_once(config, state, do_full_scan)
            if do_full_scan:
                last_full_scan = now
        except Exception as e:
            log.exception(f"폴링 중 오류 발생: {e}")
        time.sleep(cancel_check_interval)


if __name__ == "__main__":
    main()
