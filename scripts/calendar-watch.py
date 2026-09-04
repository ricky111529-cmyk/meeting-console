# /// script
# requires-python = ">=3.11"
# dependencies = ["icalendar", "recurring-ical-events", "python-dateutil"]
# ///
"""구글 캘린더(ICS) 감시 → 미팅 시작 시각에 녹음 자동 시작.

launchd가 1분마다 실행한다. 하는 일:
  1. ICS 비공개 주소를 받아 오늘 일정을 펼친다 (반복 일정 RRULE 포함)
  2. 지금 시작하는 일정 중 회의실이 잡힌 것만 scripts/record.sh 로 녹음한다
     (점심·휴가처럼 회의실 없는 일정은 건너뛴다. 제목에 [녹음]을 넣으면 강제 녹음)
  3. 일정 길이만큼 녹음하고 자동 종료된다
  4. 맥 알림을 띄워 녹음이 시작된 것을 알린다

설정 파일 (둘 다 Git 미추적):
  .claude/calendar-recorder/ics-url.txt   구글 캘린더 비공개 ICS 주소
  .claude/calendar-recorder/state.json    이미 처리한 일정 (중복 녹음 방지)

수동 확인:
  uv run scripts/calendar-watch.py --dry     지금 무엇을 녹음할지만 출력
  uv run scripts/calendar-watch.py --today   오늘 일정 전체 출력
"""

import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

REPO = Path(__file__).resolve().parent.parent
CONF_DIR = REPO / ".claude" / "calendar-recorder"
URL_FILE = CONF_DIR / "ics-url.txt"
STATE_FILE = CONF_DIR / "state.json"
LOG_FILE = CONF_DIR / "watch.log"
# 마지막 실행 시각. 일정이 없는 날은 로그가 안 쌓이므로 에이전트 생존 확인용으로 쓴다
# (매 분 로그를 남기면 하루 1,440줄이 되어 정작 중요한 기록이 묻힌다)
HEARTBEAT_FILE = CONF_DIR / "last-run.txt"
TZ = ZoneInfo("Asia/Seoul")

# 일정 시작 시각을 이 창 안에서 잡는다. launchd가 1분마다 도니 2분이면 놓치지 않는다.
CATCH_BEFORE = timedelta(seconds=60)
# 시작 후 15분까지 잡는다 (2026-08-26 확대. 이전 2분).
#  회의실로 이동하며 노트북을 덮으면 맥이 잠들고, 그 3~5분 사이에 시작 창이 지나가
#  일정 자체가 통째로 누락됐다 (8/25 13:30, 8/26 15:00 연속 발생).
#  잠자기 자체는 막을 수 없으므로(caffeinate -i로도 클램셸 잠자기는 안 막힌다)
#  깨어난 뒤에 발견해서 남은 시간만이라도 녹음하는 쪽으로 바꿨다.
#  앞부분이 빠진 녹음은 late-start.txt로 표시해 노트에서 오해하지 않게 한다.
CATCH_AFTER = timedelta(minutes=15)
LATE_THRESHOLD = timedelta(seconds=90)   # 이보다 늦게 시작하면 "앞부분 없음"으로 표시
# 앞 녹음이 이 시간 안에 끝나면 "연속 회의"로 보고 다음 사이클에 재시도한다.
#  이보다 많이 남았으면 같은 시간대에 겹친 일정으로 보고 포기한다 (마이크가 하나라 내용이 같다).
HANDOVER_WINDOW = 300                    # 5분
MAX_DURATION = timedelta(hours=3)   # 종일 일정 오인식 등으로 무한 녹음되는 것 방지
MIN_DURATION = timedelta(minutes=5)


def log(msg: str) -> None:
    stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    # 30일 넘은 기록은 버린다
    cutoff = (datetime.now(TZ) - timedelta(days=30)).isoformat()
    state = {k: v for k, v in state.items() if v.get("at", "") > cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def slugify(summary: str, start: datetime) -> str:
    """일정 제목 → record.sh가 받는 slug (영문 소문자·숫자·하이픈만).

    시작 시각을 항상 앞에 붙인다. 이유:
      - 한글 제목은 ASCII가 남지 않아 slug가 비거나 뭉개진다
        ("주간 유저인터뷰" → "user-interview", "UX 리서치 정기회의" → "ux")
      - 같은 날 여러 회의의 slug가 겹쳐 한 폴더에 섞이는 것을 막는다
      - 하루 안에서 시간순으로 정렬된다
    """
    parts = re.findall(r"[A-Za-z0-9]+", summary or "")
    base = "-".join(p.lower() for p in parts)[:28].strip("-")
    return f"{start.strftime('%H%M')}-{base}" if base else f"{start.strftime('%H%M')}-mtg"


def fetch_events(url: str, day: datetime):
    raw = urllib.request.urlopen(url, timeout=30).read()
    cal = icalendar.Calendar.from_ical(raw)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return recurring_ical_events.of(cal).between(start, start + timedelta(days=1))


def _exclude_titles() -> list:
    """자동 녹음 제외 제목 목록. 파일이 없으면 빈 목록."""
    f = REPO / ".claude" / "calendar-recorder" / "exclude-titles.txt"
    try:
        return [l.strip() for l in f.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
    except FileNotFoundError:
        return []


def should_record(ev) -> tuple[bool, str]:
    """녹음 대상인지 판정.

    기준은 **회의실(LOCATION) 예약 여부**다 (2026-08-19 추가).
    사내 회의는 회의실을 잡고 열리므로 LOCATION이 채워진다.
    점심·휴가처럼 회의가 아닌 일정은 LOCATION이 비어 있다.
    실측(8/12~8/20): 회의실 있는 11건은 전부 실제 회의, 없는 3건은 런치 2건과 휴가였다.

    제목에 [녹음]을 넣으면 회의실이 없어도 녹음한다 (외부 미팅 등 예외용).
    """
    summary = str(ev.get("SUMMARY") or "")
    if "[녹음]" in summary:
        return True, "제목에 [녹음] 표시"
    # 제외 목록 (2026-09-04 추가): .claude/calendar-recorder/exclude-titles.txt 에 적힌 제목은
    # 회의실이 있어도 녹음하지 않는다. 배포 시연처럼 녹음해도 내용이 안 잡히는 일정용.
    for pat in _exclude_titles():
        if pat in summary:
            return False, f"제외 목록: {pat}"
    location = str(ev.get("LOCATION") or "").strip()
    if not location:
        return False, "회의실 없음 (회의가 아닌 일정으로 판단)"
    return True, location


def _parse_etime(t: str) -> int:
    """ps의 etime을 초로. "MM:SS" · "HH:MM:SS" · "DD-HH:MM:SS" 형식."""
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        days = int(d)
    parts = [int(x) for x in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def current_recording():
    """지금 녹음 중인 것의 (폴더명, 남은 초). 없으면 None. 남은 초를 모르면 None.

    프로세스 존재 여부만 보면 안 되는 이유 (2026-09-01 실제로 겪음):
      - **연속 회의**: 10시 회의 녹음이 11:00:02에 끝나는데 11시 회의 발동은 10:59:57이었다.
        5초 차이로 "이미 녹음 중"이 되어 11시 회의를 통째로 놓쳤다.
      - **같은 시각 겹침**: 반대로 첫 recorder가 아직 뜨는 중이면 False가 나와 둘 다 녹음된다
        (2026-08-27 UX리서치 + Re:Sync).
    그래서 "무엇을 얼마나 더 녹음하는지"까지 알아야 한다.
    """
    r = subprocess.run(["pgrep", "-f", "scripts/bin/recorder"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    for pid in r.stdout.split():
        ps = subprocess.run(["ps", "-p", pid, "-o", "etime=,command="],
                            capture_output=True, text=True)
        line = ps.stdout.strip()
        if not line or "bin/recorder" not in line:
            continue
        etime, _, cmd = line.partition(" ")
        args = cmd.split()
        # recorder <audio경로> [duration]
        paths = [a for a in args if a.endswith(".m4a")]
        if not paths:
            continue
        folder = Path(paths[0]).parent.name
        remaining = None
        if args and args[-1].isdigit():
            try:
                remaining = max(0, int(args[-1]) - _parse_etime(etime))
            except ValueError:
                remaining = None
        return folder, remaining
    return None


def notify(title: str, message: str) -> None:
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def load_name_map() -> dict:
    """docs/meetings/vocab.txt의 "이메일 = 이름" 줄을 읽는다."""
    f = REPO / "docs" / "meetings" / "vocab.txt"
    m = {}
    if not f.exists():
        return m
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line or "@" not in line:
            continue
        email, _, name = line.partition("=")
        m[email.strip().lower()] = name.strip()
    return m


def write_attendees(ev, date_str: str, slug: str, summary: str) -> None:
    """캘린더 참석자를 폴더에 남긴다 (2026-08-26 추가).

    이걸 안 남기면 노트를 쓸 때 참석자를 STT 원문에서 추측해야 한다.
    원문에서는 이름이 여러 표기로 흩어져(예: 철수/철쑤) 특정이 안 됐다.

    ⚠️ ICS는 **초대받은 사람**을 알려준다. 실제 참석은 알려주지 않는다.
       DECLINED만 확실히 제외할 수 있고, NEEDS-ACTION은 응답을 안 한 것이지 불참이 아니다.
       그래서 응답 상태를 그대로 적고 판정은 사람에게 맡긴다.
    """
    atts = ev.get("ATTENDEE")
    if not atts:
        return
    if not isinstance(atts, list):
        atts = [atts]

    names = load_name_map()
    rows = []
    for a in atts:
        email = str(a).replace("mailto:", "").strip()
        if "resource.calendar.google.com" in email:
            continue          # 회의실 예약은 사람이 아니다
        stat = str(a.params.get("PARTSTAT", "")) or "UNKNOWN"
        cn = str(a.params.get("CN", "") or "").strip()
        if cn.lower() == email.lower():
            cn = ""          # Google은 이름이 없으면 CN에 이메일을 그대로 넣는다
        who = names.get(email.lower()) or cn
        rows.append((stat, who, email))
    if not rows:
        return

    order = {"ACCEPTED": 0, "NEEDS-ACTION": 1, "TENTATIVE": 2, "DECLINED": 3}
    rows.sort(key=lambda r: (order.get(r[0], 9), r[2]))

    lines = [
        f"> 캘린더 참석자 (자동 기록) | {summary} | {date_str}",
        "> ⚠️ **초대 명단이다. 실제 참석 여부가 아니다.**",
        ">    DECLINED는 불참이 확실하지만, NEEDS-ACTION은 응답을 안 한 것일 뿐 참석했을 수 있다.",
        "> 이름이 빈 칸은 docs/meetings/vocab.txt에 이메일 매핑이 없는 계정이다. 추측해서 채우지 말 것.",
        "",
        "| 응답 | 이름 | 계정 |",
        "|---|---|---|",
    ]
    for stat, who, email in rows:
        lines.append(f"| {stat} | {who or '(미확인)'} | {email} |")
    lines.append("")

    d = REPO / "docs" / "meetings" / f"{date_str}_{slug}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "attendees.md").write_text("\n".join(lines), encoding="utf-8")


def start_recording(slug: str, date_str: str, seconds: int, summary: str,
                   late_min: int = 0) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    out = (CONF_DIR / f"record-{date_str}-{slug}.log").open("a", encoding="utf-8")
    subprocess.Popen(
        [str(REPO / "scripts" / "record.sh"), slug, date_str, "--duration", str(seconds)],
        cwd=str(REPO), stdout=out, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    late = f" ⚠️ {late_min}분 늦게 시작 (앞부분 없음)" if late_min else ""
    log(f"녹음 시작: {summary} → docs/meetings/{date_str}_{slug}/ ({seconds // 60}분){late}")

    # 앞부분이 빠졌으면 폴더에 표시를 남긴다. 노트를 쓸 때 이 파일이 보여야
    #  "회의 앞 N분이 녹음에 없다"는 것을 모르고 넘어가지 않는다.
    if late_min:
        d = REPO / "docs" / "meetings" / f"{date_str}_{slug}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "late-start.txt").write_text(
            f"이 녹음은 회의 시작 후 약 {late_min}분 지점부터 시작됐습니다.\n"
            f"앞 {late_min}분은 녹음에 없습니다. 노트에 그 사실을 적으세요.\n"
            f"원인: 시작 시각에 맥이 잠자기 상태였고, 깨어난 뒤 뒤늦게 발동했습니다.\n",
            encoding="utf-8")

    notify("미팅 녹음 시작",
           f"{summary}\n{seconds // 60}분 예정. 참석자에게 고지하세요.\n"
           + (f"⚠️ {late_min}분 늦게 시작 - 앞부분이 없습니다"
              if late_min else "⚠️ 노트북을 열어두세요 (닫으면 녹음이 끊깁니다)"))


def main() -> int:
    dry = "--dry" in sys.argv
    show_today = "--today" in sys.argv

    if not URL_FILE.exists():
        log(f"ICS 주소 파일 없음: {URL_FILE.relative_to(REPO)}")
        log("구글 캘린더 > 설정 > 내 캘린더 설정 > '비공개 주소의 iCal 형식' URL을 이 파일에 저장하세요.")
        return 1
    url = URL_FILE.read_text(encoding="utf-8").strip()
    if not url.startswith("http"):
        log("ICS 주소가 http로 시작하지 않습니다.")
        return 1

    now = datetime.now(TZ)
    try:
        events = fetch_events(url, now)
    except Exception as e:
        log(f"ICS 조회 실패: {e}")
        return 1

    if not dry and not show_today:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(
            f"{now:%Y-%m-%d %H:%M:%S} / 오늘 일정 {len(events)}건 조회\n", encoding="utf-8")

    if show_today:
        print(f"오늘({now:%Y-%m-%d}) 일정 {len(events)}건")
        for ev in sorted(events, key=lambda e: str(e.get("DTSTART").dt)):
            s = ev.get("DTSTART").dt
            e_ = ev.get("DTEND").dt if ev.get("DTEND") else None
            allday = not isinstance(s, datetime)
            when = "종일" if allday else f"{s.astimezone(TZ):%H:%M}~{e_.astimezone(TZ):%H:%M}"
            ok, reason = should_record(ev)
            mark = "🔴 녹음" if (ok and not allday) else "—     "
            print(f"  {mark} {when}  {ev.get('SUMMARY')}")
            if not ok or allday:
                print(f"          건너뜀: {'종일 일정' if allday else reason}")
        return 0

    state = load_state()
    fired = 0

    # 창이 15분이라 잠자기에서 깨어나면 여러 일정이 동시에 걸릴 수 있다.
    #  가장 최근에 시작한 것부터 보고, 한 사이클에 하나만 띄운다 (아래 break).
    def _start_key(ev):
        d = ev.get("DTSTART").dt
        return d.astimezone(TZ) if isinstance(d, datetime) else datetime.min.replace(tzinfo=TZ)

    for ev in sorted(events, key=_start_key, reverse=True):
        dtstart = ev.get("DTSTART").dt
        if not isinstance(dtstart, datetime):
            continue  # 종일 일정은 녹음 대상 아님
        dtend = ev.get("DTEND").dt if ev.get("DTEND") else dtstart + timedelta(hours=1)
        if not isinstance(dtend, datetime):
            continue

        start = dtstart.astimezone(TZ)
        end = dtend.astimezone(TZ)

        # 지금 시작하는 일정인가
        if not (start - CATCH_BEFORE <= now <= start + CATCH_AFTER):
            continue
        # 창이 넓어진 만큼, 이미 끝난 일정을 뒤늦게 잡는 일은 막아야 한다
        #  (짧은 회의는 15분 창 안에서 끝나 있을 수 있다)
        if now >= end:
            continue

        summary = str(ev.get("SUMMARY") or "제목 없음")
        uid = f"{ev.get('UID')}@{start.isoformat()}"
        if uid in state:
            continue

        ok, reason = should_record(ev)
        if not ok:
            log(f"건너뜀: {summary} - {reason}")
            state[uid] = {"at": now.isoformat(), "skipped": reason, "summary": summary}
            continue

        # 이미 시작한 만큼은 빼고, 남은 시간을 녹음한다
        remaining = end - now
        remaining = max(MIN_DURATION, min(remaining, MAX_DURATION))
        slug = slugify(summary, start)
        date_str = start.strftime("%Y-%m-%d")

        if dry:
            print(f"[dry] {summary} ({start:%H:%M}~{end:%H:%M}) "
                  f"→ {date_str}_{slug}, {int(remaining.total_seconds()) // 60}분")
            fired += 1
            continue

        cur = current_recording()
        if cur:
            folder, remaining = cur
            if folder == f"{date_str}_{slug}":
                continue        # 이 회의를 이미 녹음 중 (중복 발동)
            if remaining is not None and remaining <= HANDOVER_WINDOW:
                # 앞 회의가 곧 끝난다 = 연속 회의다.
                #  state에 적지 않고 넘어가면 다음 사이클(60초 뒤)에 다시 시도한다.
                #  발동 창이 15분이라 앞 녹음이 끝나는 대로 잡힌다.
                log(f"앞 녹음이 {remaining}초 남아 대기: {summary} (다음 사이클 재시도)")
                continue
            log(f"이미 녹음 중이라 건너뜀: {summary} "
                f"({folder}, 남은 {remaining if remaining is not None else '?'}초)")
            state[uid] = {"at": now.isoformat(), "skipped": "already-recording",
                          "summary": summary, "blocked_by": folder}
            continue

        late = now - start
        late_min = int(late.total_seconds() // 60) if late >= LATE_THRESHOLD else 0
        start_recording(slug, date_str, int(remaining.total_seconds()), summary, late_min)
        write_attendees(ev, date_str, slug, summary)
        # 한 사이클에 하나만 띄운다. 같은 시각 일정이 여러 개면 첫 recorder가 아직 뜨지 않아
        #  current_recording()이 None을 돌려주고 둘 다 녹음되는 문제가 있었다 (2026-08-27).
        state[uid] = {"at": now.isoformat(), "slug": slug, "summary": summary}
        fired += 1
        break

    if not dry:
        save_state(state)
    if fired == 0 and (dry or "--verbose" in sys.argv):
        print("지금 시작하는 일정 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
