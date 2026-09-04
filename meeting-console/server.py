# /// script
# requires-python = ">=3.11"
# dependencies = ["icalendar", "recurring-ical-events", "python-dateutil"]
# ///
"""회의 콘솔 로컬 웹 서버 - 제어판 · 검수 화면 · 화자 등록.

표준 라이브러리 http.server 만 쓴다 (스펙 3-1). 프레임워크도 빌드 도구도 없다.
팀원이 각자 설치하는 것이 목표라 런타임이 늘수록 실패 지점이 는다.

  실행: uv run outputs/vibe-coding/meeting-console/server.py
        브라우저가 자동으로 열린다. 끄려면 Ctrl+C.

**바깥으로 나가는 통신이 없다.** 이 파일에는 네트워크 클라이언트 코드가 한 줄도 없다.
캘린더(ICS) 조회는 기존 scripts/calendar-watch.py 의 함수(fetch_events · should_record ·
slugify)를 import 해서 부르는 것으로만 이뤄지고, 그 주소는 .claude/calendar-recorder/ics-url.txt
에서 온다 (스펙 3-6, 수용 기준 3). 주소 원문은 화면·로그·API 응답 어디에도 싣지 않는다.
오디오와 회의 내용은 이 맥을 떠나지 않는다.
"""
from __future__ import annotations

import importlib.util
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meeting_state as ms  # noqa: E402
import setup_wizard as sw  # noqa: E402

HOST = "127.0.0.1"          # 고정. 다른 기기에서 붙을 수 없다
PORT_START = 7788
PORT_TRIES = 10
STATIC = ms.CONSOLE / "static"
RECORDER_LABEL = "com.meeting-console.meeting-recorder"
RECORDER_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{RECORDER_LABEL}.plist"
CLIP_MAX_SEC = 20.0         # 재생용 클립 상한. 한 블록이 몇 분씩 이어지기도 한다
UV = shutil.which("uv") or "uv"

TOKEN = ""
TOKEN_FILE: Path | None = None       # 기동해서 포트를 잡은 뒤에 정해진다 (ms.token_path)
MY_PORT = 0                          # 이 인스턴스가 잡은 포트
_today_lock = threading.Lock()      # ICS 조회 직렬화 (day_schedule)
_enroll: dict[str, dict] = {}       # 폴더 -> 분리 작업 상태 (등록 · 재분리 공용)
_jobs_lock = threading.Lock()
JOBS_FILE = ms.STATE_DIR / "jobs.json"
BACKUP_DIR = ms.STATE_DIR / "enroll-backup"


def set_job(folder: str, **fields) -> dict:
    """분리 작업 상태를 바꾸고 디스크에도 남긴다.

    디스크에 남기는 이유는 서버가 도중에 꺼질 수 있어서다 (스펙 7절 위험 2). 재분리는 수 분
    걸리는데 그 사이 서버를 끄면 메모리에만 있던 상태가 사라져 화면이 "진행 중 아님"으로 보이고,
    반쯤 쓰인 분리본이 남는다. 기동 때 resume_jobs() 가 이 파일을 읽어 정리한다.
    """
    with _jobs_lock:
        cur = dict(_enroll.get(folder, {}))
        cur.update(fields)
        _enroll[folder] = cur
        ms.write_json(JOBS_FILE, _enroll)
    return cur


def busy_folders() -> list[str]:
    """지금 diarize.py 가 도는 폴더. 등록부 편집 잠금 판정에 쓴다 (수용 기준 35)."""
    return [f for f, j in _enroll.items() if j.get("state") == "running"]


def orphan_diarize(folder: str, grace: float = 5.0) -> list[int]:
    """그 회의 폴더를 대상으로 도는 diarize.py 를 찾아 끊는다. 끊은 pid 목록 (스펙 7절 위험 2).

    명령줄에 폴더 경로가 들어 있는 것만 고른다. 다른 회의의 분리를 끊으면 안 된다.
    SIGTERM 을 먼저 주고 안 죽으면 SIGKILL 한다 (diarize.py 는 파일을 마지막에 한 번 쓴다).
    """
    r = run(["ps", "-Ao", "pid=,command="])
    out_path = str(ms.MEETINGS / folder / "transcript-speakers.md")
    pids = []
    for line in r.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if not pid.isdigit():
            continue
        try:
            argv = shlex.split(cmd)
        except ValueError:
            continue
        # ⚠️ 조건을 셋 다 만족해야 끊는다. 문자열 포함만 보면 **엉뚱한 프로세스를 죽인다**
        #    (실측: 검증 중인 셸의 명령줄에 두 문자열이 다 들어 있어 그 셸이 끊겼다).
        #    실행 파일이 python·uv 이고, 인자에 diarize.py 가 통째로 있고, 결과 파일 경로가
        #    인자로 정확히 일치할 때만 이 회의의 분리 프로세스로 본다.
        exe = Path(argv[0]).name.lower() if argv else ""
        # 프레임워크 파이썬은 실행 파일 이름이 "Python" 이고 uv 가 만든 것은 "python3.14" 다
        if not (exe.startswith("python") or exe == "uv"):
            continue
        if not any(a.endswith("diarize.py") and "=" not in a for a in argv[1:]):
            continue
        if out_path not in argv[1:]:
            continue
        pids.append(int(pid))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = []
        for pid in pids:
            try:
                os.kill(pid, sig)
                alive.append(pid)
            except OSError:
                pass
        if not alive:
            break
        time.sleep(grace if sig == signal.SIGTERM else 0.5)
    return pids


def resume_jobs() -> None:
    """서버가 꺼져 끊긴 작업을 기동 때 정리한다 (스펙 7절 위험 2).

    **정한 것**: 재분리·등록 도중 서버가 꺼지면 그 작업은 끝난 것으로 보지 않는다. 백업해 둔
    직전 분리본으로 되돌리고 상태를 "중단됨"으로 남긴다. 사람이 화면에서 그 사실을 보고 다시
    누르게 하는 쪽이, 반쯤 갱신된 분리본을 정상으로 보여주는 것보다 안전하다.
    화면을 닫는 것은 아무 영향이 없다. 작업은 서버 스레드에서 돌고 상태는 여기 남는다.
    """
    peers = ms.live_token_ports(exclude=MY_PORT)
    if peers:
        # 같은 STATE_DIR 의 다른 서버가 살아 있으면 그 서버의 진행 중 작업을 실패로 만들지 않는다.
        #  (실측: 검증 서버를 띄웠더니 앞 서버에서 돌던 재분리가 failed 로 바뀌고 백업이 덮였다)
        _enroll.update(ms.read_json(JOBS_FILE, {}) or {})
        print(f"▸ 같은 .state 를 쓰는 서버가 떠 있어({peers}) 끊긴 작업 정리를 건너뜁니다", flush=True)
        return
    old = ms.read_json(JOBS_FILE, {}) or {}
    for folder, job in old.items():
        if not (ms.MEETINGS / folder).is_dir():    # 지워진 회의의 기록은 들고 있지 않는다
            continue
        if job.get("state") != "running":
            _enroll[folder] = job
            continue
        backup = BACKUP_DIR / f"{folder}-transcript-speakers.md"
        spk = ms.MEETINGS / folder / "transcript-speakers.md"
        note = ""
        # ⚠️ 서버를 SIGKILL 로 끊으면 자식 diarize.py 는 살아남는다 (60분 회의는 수 분 걸린다).
        #    백업을 먼저 되돌리면 그 뒤에 고아가 결과를 덮어 되돌린 것이 무효가 된다. 순서를
        #    고정하려고 **자식을 먼저 끊고** 되돌린다. 이어받지 않는 이유는 그 결과가 온전한지
        #    판정할 방법이 없어서다 (반쯤 쓰인 분리본을 정상으로 보여주지 않는다는 결정과 같다).
        killed = orphan_diarize(folder)
        if killed:
            note = f" 남아 있던 화자 분리 프로세스({', '.join(map(str, killed))})를 먼저 끊었습니다."
        if backup.exists() and spk.parent.exists():
            shutil.copy2(backup, spk)
            backup.unlink(missing_ok=True)
            note += " 직전 분리본으로 되돌렸습니다."
        _enroll[folder] = {**job, "state": "failed",
                           "message": f"서버가 꺼져 {job.get('kind', '분리')} 작업이 중단됐습니다.{note}"
                                      " 다시 실행하세요"}
    ms.write_json(JOBS_FILE, _enroll)


# ---------------------------------------------------------------- 작은 도우미

def pct(s: str) -> str:
    """퍼센트 디코딩. urllib 를 쓰지 않는다 (이 파일에 네트워크 관련 import 를 두지 않으려고)."""
    b = s.replace("+", " ").encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(b):
        if b[i] == 0x25 and len(b) - i >= 3:
            try:
                out.append(int(b[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(b[i])
        i += 1
    return out.decode("utf-8", "replace")


def parse_query(qs: str) -> dict:
    out = {}
    for part in qs.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        out[pct(k)] = pct(v)
    return out


def run(cmd: list[str], timeout: int = 30, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          cwd=str(cwd) if cwd else None)


def safe_folder(name: str) -> str | None:
    """폴더명 검증. 경로 탈출과 엉뚱한 폴더 삭제를 막는 최소 장치."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    if not (ms.MEETINGS / name).is_dir():
        return None
    return name


# ---------------------------------------------------------------- 캘린더 조회 (스펙 3-6)

CW_PATH = ms.SCRIPTS / "calendar-watch.py"
ICS_URL_FILE = ms.REPO / ".claude" / "calendar-recorder" / "ics-url.txt"
_cw = None                          # import 한 calendar-watch 모듈
_cw_error = ""


def calendar_watch():
    """scripts/calendar-watch.py 를 모듈로 불러온다 (스펙 3-6).

    **판정을 다시 구현하지 않는다.** 녹음 대상 판정(should_record)과 폴더명 계산(slugify)이
    스크립트와 콘솔에서 갈리면 화면이 실제 녹음과 다른 말을 한다. 파일명에 하이픈이 있어
    보통 import 문으로는 안 잡히므로 importlib 로 경로를 지정해 부른다.

    서브프로세스 `--today` 를 쓰지 않는 이유는 그 플래그가 오늘만 출력하기 때문이다.
    캘린더 페이지는 지난 주도 봐야 한다.
    """
    global _cw, _cw_error
    if _cw is not None or _cw_error:
        return _cw
    try:
        spec = importlib.util.spec_from_file_location("calendar_watch", CW_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)                    # 모듈 최상단은 정의뿐이다 (main 은 __main__ 가드)
        _cw = mod
    except Exception as exc:                            # noqa: BLE001
        _cw_error = f"calendar-watch.py 를 불러오지 못했습니다: {type(exc).__name__}: {exc}"
    return _cw


def ics_url() -> str:
    """ICS 주소를 파일에서 읽는다. **이 값은 어디에도 되돌려주지 않는다** (수용 기준 3·42).

    사용자 개인 구글 캘린더의 비밀 주소라 화면·로그·API 응답에 실리면 그대로 새어 나간다.
    """
    if not ICS_URL_FILE.exists():
        raise RuntimeError("ICS 주소 파일이 없습니다 (.claude/calendar-recorder/ics-url.txt)")
    url = ICS_URL_FILE.read_text(encoding="utf-8").strip()
    if not url.startswith("http"):
        raise RuntimeError("ICS 주소가 http 로 시작하지 않습니다")
    return url


def safe_reason(exc: Exception) -> str:
    """오류 문구에서 ICS 주소를 지운다.

    urllib 는 실패하면 예외 문구에 URL 을 통째로 넣는다. 그것을 그대로 화면에 띄우면
    비밀 주소가 노출된다 (수용 기준 42 는 응답 본문을 grep 해서 이것을 본다).
    """
    msg = f"{type(exc).__name__}: {exc}"
    try:
        url = ics_url()
    except Exception:                                   # noqa: BLE001
        url = ""
    if url:
        msg = msg.replace(url, "(ICS 주소)")
    # 주소 일부만 새는 경우까지 막는다 (쿼리·경로 조각).
    msg = re.sub(r"https?://\S+", "(ICS 주소)", msg)
    return msg[:300]


# 설치 마법사에 calendar-watch 로더 · 오류 문구 세척기 · ICS 저장 경로를 넘긴다.
#  마법사가 ICS 조회 경로를 따로 만들지 않게 하려는 것이다 (수용 기준 3·45).
sw.bind(calendar_watch, safe_reason, ICS_URL_FILE)


def event_dict(ev, cw) -> dict:
    """ICS 일정 하나를 화면이 쓰는 형태로. 판정은 전부 calendar-watch 함수가 한다."""
    s = ev.get("DTSTART").dt
    e = ev.get("DTEND").dt if ev.get("DTEND") else None
    all_day = not isinstance(s, datetime)
    summary = str(ev.get("SUMMARY") or "")
    ok, reason = cw.should_record(ev)
    if all_day:
        # --today 와 같은 규칙이다: 종일 일정은 회의실이 있어도 녹음하지 않는다.
        ok, reason = False, "종일 일정"
    start = s.astimezone(cw.TZ) if isinstance(s, datetime) else None
    end = e.astimezone(cw.TZ) if isinstance(e, datetime) else None
    when = "종일" if all_day else f"{start:%H:%M}~{end:%H:%M}" if end else f"{start:%H:%M}"
    return {
        "when": when,                                   # 옛 캐시 형식과 같은 키를 유지한다
        "title": summary,
        "record": ok,
        "skip_reason": "" if ok else reason,
        # 회의실. should_record 가 True 일 때의 reason 이 LOCATION 이다 (스펙 3-6, 1단계 유보분).
        "room": str(ev.get("LOCATION") or "").strip(),
        "all_day": all_day,
        "date": (start or datetime.now(cw.TZ)).strftime("%Y-%m-%d") if not all_day
                else str(s),
        "start": start.strftime("%H:%M") if start else "",
        "end": end.strftime("%H:%M") if end else "",
        "start_ts": start.timestamp() if start else 0.0,
        "end_ts": end.timestamp() if end else 0.0,
        # 폴더명은 slugify 로 계산한다. 매핑이 결정적이라 별도 인덱스가 필요 없다 (스펙 3-6).
        "folder": f"{start:%Y-%m-%d}_{cw.slugify(summary, start)}" if start else "",
        "attendees": attendee_rows(ev, cw),
    }


def attendee_rows(ev, cw) -> list[dict]:
    """참석자 응답. 폴더가 없는 일정은 attendees.md 가 없어 여기서만 볼 수 있다.

    **calendar-watch.write_attendees 를 부르지 않는다.** 그 함수는 docs/meetings/ 아래
    폴더를 만들고 파일을 쓴다. 캘린더를 훑어보기만 해도 회의 폴더가 생기면 안 된다.
    이름 매핑은 같은 모듈의 load_name_map 을 그대로 쓴다 (표기가 갈리지 않게).
    """
    atts = ev.get("ATTENDEE")
    if not atts:
        return []
    if not isinstance(atts, list):
        atts = [atts]
    names = cw.load_name_map()
    rows = []
    for a in atts:
        email = str(a).replace("mailto:", "").strip()
        if "resource.calendar.google.com" in email:
            continue                                    # 회의실 예약은 사람이 아니다
        stat = str(a.params.get("PARTSTAT", "")) or "UNKNOWN"
        cn = str(a.params.get("CN", "") or "").strip()
        if cn.lower() == email.lower():
            cn = ""
        rows.append({"status": stat, "name": names.get(email.lower()) or cn, "email": email})
    order = {"ACCEPTED": 0, "NEEDS-ACTION": 1, "TENTATIVE": 2, "DECLINED": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["email"]))
    return rows


def fetch_day(day: str) -> dict:
    """그 하루의 일정을 ICS 에서 받는다. 캐시 판단은 부르는 쪽(day_schedule)이 한다."""
    cw = calendar_watch()
    data = {"day": day, "at": time.time(),
            "fetched_at": datetime.now(ms.TZ).strftime("%H:%M:%S"),
            "events": [], "error": None}
    if not cw:
        data["error"] = _cw_error
        return data
    try:
        when = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=cw.TZ)
        evs = cw.fetch_events(ics_url(), when)
        data["events"] = sorted((event_dict(ev, cw) for ev in evs),
                                key=lambda e: (not e["all_day"], e["start"]))
    except Exception as exc:                            # noqa: BLE001
        data["error"] = safe_reason(exc)
    return data


def day_schedule(day: str, force: bool = False) -> dict:
    """하루치 일정. 디스크 캐시(`.state/schedule-{날짜}.json`)를 서버와 메뉴바가 공유한다.

    TTL 은 날짜에 따라 다르다.
      - 지난 날: 만료 없음. 이미 지난 일정은 바뀌지 않는다 (스펙 3-6 「과거 주는 한 번 받은 것을 유지」)
      - 오늘: 60초 (제어판이 다음 녹음 대상을 이 값으로 보여준다)
      - 앞날: 300초. 며칠 뒤 일정이 1분 단위로 정확할 필요가 없고, 조회 1회가 약 2초다

    조회는 락 하나로 직렬화한다. 없으면 캐시가 만료된 순간 동시 요청마다 ICS 를 각각
    내려받는다 (1회 1.5MB). 캘린더 페이지가 7일을 한꺼번에 부르므로 특히 중요하다.
    """
    today = datetime.now(ms.TZ).strftime("%Y-%m-%d")
    ttl = float("inf") if day < today else (60.0 if day == today else 300.0)

    cached = ms.read_schedule(day)
    if not force and ms.schedule_age(cached) < ttl:
        return cached
    with _today_lock:
        cached = ms.read_schedule(day)
        # 락을 기다리는 동안 다른 스레드가 채웠으면 그것을 쓴다.
        #  force 여도 2초 안에 방금 받아온 것이면 다시 받지 않는다 (버튼 두 번 누름).
        if ms.schedule_age(cached) < (2.0 if force else ttl):
            return cached
        data = fetch_day(day)
        # 조회가 실패해도 캐시를 쓴다. 안 쓰면 실패할 때마다 폴링 주기로 재조회한다.
        #  단 실패한 캐시는 오래 붙들지 않게 지난 날이어도 at 을 지금으로 둔다.
        if data.get("error") and cached and cached.get("events"):
            data["events"] = cached["events"]           # 마지막으로 성공한 것을 유지한다
            data["stale"] = True
        ms.write_schedule(day, data)
        ms.prune_schedules()
        return data


def today_schedule(force: bool = False) -> dict:
    """오늘 일정 (제어판·메뉴바가 쓴다)."""
    return day_schedule(datetime.now(ms.TZ).strftime("%Y-%m-%d"), force=force)


def fetch_week(days: list[str], force: bool = False) -> None:
    """한 주치 일정을 ICS **1회 다운로드**로 채운다 (스펙 8절 3단계 결정, 2026-09-04).

    하루씩 `fetch_events(url, day)` 를 부르면 같은 1.5MB ICS 를 7번 내려받아 한 주
    최초 로딩이 13~16초였다. 받은 것을 임시 파일에 한 번 두고 `file://` 로 7일을
    펼치면 실측 4.0초다 (다운로드 1.84 + 펼치기 2.16).

    - 다운로드는 `calendar-watch` 모듈의 urllib 을 그대로 쓴다. 조회 경로를 콘솔이
      따로 만들지 않는다는 수용 기준 3 의 뜻을 지키려는 것이고, 펼치기는 기존
      `cw.fetch_events` 를 그대로 부른다
    - **ICS 원문을 디스크에 남기지 않는다.** 임시 파일은 0600 으로 만들고 요청이
      끝나면 지운다 (실패해도 finally 에서 지운다). 저장 위치는 이 레포 밖이다
    - 캐시가 신선한 날은 건너뛴다. 받을 날이 하나도 없으면 다운로드도 하지 않는다
    """
    today = datetime.now(ms.TZ).strftime("%Y-%m-%d")

    def stale(day: str) -> bool:
        ttl = float("inf") if day < today else (60.0 if day == today else 300.0)
        return force or ms.schedule_age(ms.read_schedule(day)) >= ttl

    with _today_lock:
        targets = [d for d in days if stale(d)]
        if not targets:
            return
        cw = calendar_watch()
        if not cw:
            for day in targets:
                _save_day_error(day, _cw_error)
            return
        tmp_path = ""
        try:
            raw = cw.urllib.request.urlopen(ics_url(), timeout=60).read()
            fd, tmp_path = tempfile.mkstemp(prefix="mc-ics-", suffix=".ics")
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            del raw
            for day in targets:
                data = {"day": day, "at": time.time(),
                        "fetched_at": datetime.now(ms.TZ).strftime("%H:%M:%S"),
                        "events": [], "error": None}
                try:
                    when = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=cw.TZ)
                    evs = cw.fetch_events(f"file://{tmp_path}", when)
                    data["events"] = sorted((event_dict(ev, cw) for ev in evs),
                                            key=lambda e: (not e["all_day"], e["start"]))
                except Exception as exc:                # noqa: BLE001
                    data["error"] = safe_reason(exc)
                _save_day(day, data)
        except Exception as exc:                        # noqa: BLE001
            reason = safe_reason(exc)
            for day in targets:
                _save_day_error(day, reason)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)                     # ICS 원문을 남기지 않는다
        ms.prune_schedules()


def _save_day(day: str, data: dict) -> None:
    """조회 결과를 캐시에 쓴다. 실패했으면 마지막으로 성공한 것을 유지한다 (day_schedule 과 같다)."""
    cached = ms.read_schedule(day)
    if data.get("error") and cached and cached.get("events"):
        data["events"] = cached["events"]
        data["stale"] = True
    ms.write_schedule(day, data)


def _save_day_error(day: str, reason: str | None) -> None:
    _save_day(day, {"day": day, "at": time.time(),
                    "fetched_at": datetime.now(ms.TZ).strftime("%H:%M:%S"),
                    "events": [], "error": reason or "캘린더 조회에 실패했습니다"})


# ---------------------------------------------------------------- 캘린더 페이지 (스펙 5-2)

# 칸 상태 8종 (스펙 3-6). 색 구분은 static/style.css 의 같은 코드에 붙어 있다.
CAL_STATES = [
    ("not-target", "대상 아님"),
    ("planned", "녹음 예정"),
    ("recording", "녹음 중"),
    ("stt-wait", "글로 옮기는 중"),
    ("draft-wait", "노트 작성 예정"),
    ("review-wait", "확인 필요"),
    ("approved", "확정"),
    ("missing", "녹음 없음"),
]
CAL_LABEL = dict(CAL_STATES)

# 폴더 상태(ms.STATES 8종) -> 칸 상태(8종) 대응.
#  폴더 상태가 더 잘게 갈라져 있어 그대로는 칸에 안 들어간다. 정확한 폴더 상태는
#  칸의 보조 문구와 상세 패널에 그대로 남긴다 (뭉개진 채로 끝나지 않게).
FOLDER_TO_CAL = {
    "approved": "approved",
    "review": "review-wait",
    "suspect": "review-wait",       # 사람이 봐야 하는 것이라 확인 필요와 같은 자리에 둔다
    "drafting": "draft-wait",
    "failed": "draft-wait",         # 초안이 아직 없다는 점에서 노트 작성 예정와 같다
    "draft-wait": "draft-wait",
    "stt-wait": "stt-wait",
    "excluded": "not-target",       # 사람이 제외한 것. 흐리게 두는 자리가 맞다
}

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def monday_of(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def week_days(start: str) -> list[str]:
    d = datetime.strptime(monday_of(start), "%Y-%m-%d")
    return [(d + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]


def catch_after_sec() -> float:
    """일정이 시작한 뒤 이 시간까지는 아직 녹음이 붙을 수 있다.

    calendar-watch 의 CATCH_AFTER 를 그대로 쓴다 (지금 15분). 이 값을 안 쓰고 시작 시각만
    보면, 방금 시작한 회의가 녹음 시작 전 몇 초 동안 "녹음 없음"으로 깜빡인다.
    """
    cw = calendar_watch()
    try:
        return cw.CATCH_AFTER.total_seconds()
    except Exception:                                   # noqa: BLE001
        return 15 * 60


def claimed_folders(events: list[dict], folders: set[str]) -> set[str]:
    """다른 일정이 **정확 일치**로 이미 가져간 폴더.

    접두사 매칭(`match_folder` 2순위)의 후보에서 빼려고 먼저 센다. 같은 시각에 두 일정이
    있고 폴더가 하나뿐일 때, 폴더 임자가 아닌 쪽이 접두사로 그것을 빌려가 「확정」으로
    떠서 자기 놓침을 감추는 일이 있었다. 정확 일치가 언제나 접두사보다 세다.
    """
    return {f for ev in events if (f := ev.get("folder") or "") in folders}


def match_folder(ev: dict, folders: set[str], claimed: set[str] | None = None) -> tuple[str, bool]:
    """일정에 대응하는 회의 폴더. (폴더명, 예상 이름과 같은가)

    1순위는 `slugify` 로 계산한 이름 그대로다 (스펙 3-6).
    없으면 **같은 날 같은 시각(`{날짜}_{HHMM}-`)으로 시작하는 폴더**를 찾는다.
    slugify 는 시작 시각을 항상 앞에 붙이므로 이 접두사도 결정적이다.

    이 2순위가 필요한 이유는 실측이다. 지난 주 일정 10건 중 2건이 "녹음 없음"으로 떴는데
    녹음은 되어 있었다. 폴더명은 **녹음한 시점의 제목**으로 만들어지고 그 뒤에 캘린더에서
    제목을 고치면 slug 가 갈린다 (`1330-1-on-1` vs `1330-1-on-1-siwon`,
    `1330-re-sync` vs `1330-aip-ux`). 시각까지 같은 폴더를 남으로 보면 놓침이 아닌 것을
    놓침으로 세고, 정작 실제 놓침이 묻힌다.
    """
    folder = ev.get("folder") or ""
    if not folder:
        return "", False
    if folder in folders:
        return folder, True
    prefix = folder[:16]                                # "YYYY-MM-DD_HHMM-"
    if len(prefix) == 16:
        taken = claimed or set()
        for cand in sorted(folders):
            # 다른 일정이 정확 일치로 점유한 폴더는 빌리지 않는다. 빌리면 그 일정의
            #  녹음을 자기 것처럼 보여주고 자기 「놓침」이 화면에서 사라진다.
            if cand.startswith(prefix) and cand not in taken:
                return cand, False
    return "", False


def backfill(e: dict, day: str) -> dict:
    """옛 형식 캐시에 없는 값을 메운다.

    옛 메뉴바(1단계 코드)는 `--today` 출력을 파싱해 when · title · record · skip_reason 만
    캐시에 넣었다. 그 캐시를 새 코드가 읽으면 시작 시각을 몰라 **앞으로 열릴 회의가
    「녹음 없음」으로 뜬다** (놓침 경고가 거짓으로 울린다). 시각 문구에서 시작 시각을 되살린다.
    폴더명은 메우지 않는다 (없는 이름을 지어내면 엉뚱한 폴더를 가리킨다).
    """
    if e.get("start_ts") or not day:
        return e
    m = re.match(r"^(\d{1,2}):(\d{2})", str(e.get("when") or ""))
    if not m:
        return e
    try:
        start = datetime.strptime(day, "%Y-%m-%d").replace(
            hour=int(m.group(1)), minute=int(m.group(2)), tzinfo=ms.TZ)
    except ValueError:
        return e
    e["start_ts"] = start.timestamp()
    e["start"] = f"{start:%H:%M}"
    return e


def decorate_events(events: list[dict], folders: set[str], rec_folder: str,
                    day: str = "") -> list[dict]:
    """일정 칸에 상태 8종을 붙인다 (스펙 3-6 표).

    판정 순서가 중요하다. **녹음 중을 폴더 상태보다 먼저 본다.** 녹음 중인 폴더에는 오디오만
    있어 폴더 상태로는 "글로 옮기는 중"로 읽힌다.
    """
    now = time.time()
    grace = catch_after_sec()
    # 정확 일치를 먼저 전부 배정하고, 남은 폴더에 대해서만 접두사 매칭을 한다
    claimed = claimed_folders(events, folders)
    out = []
    for ev in events:
        # ⚠️ 캐시 파일은 다른 버전이 쓴 것일 수 있다 (옛 메뉴바가 쓰던 형식에는 room · folder ·
        #    start_ts 가 없다). 없는 키를 그대로 읽으면 서버가 500 으로 죽으므로 기본값을 깐다.
        e = {"when": "", "title": "", "record": False, "skip_reason": "", "room": "",
             "all_day": False, "start": "", "end": "", "start_ts": 0.0, "end_ts": 0.0,
             "folder": "", "attendees": [], **dict(ev)}
        e = backfill(e, day)
        e["expected_folder"] = e.get("folder") or ""
        folder, exact = match_folder(e, folders, claimed)
        e["exists"] = bool(folder)
        e["note"] = ""
        if folder:
            e["folder"] = folder                        # 실제로 있는 폴더를 가리킨다
            if not exact:
                # 예상 이름과 다르면 그 사실을 적는다. 조용히 갈아치우면 왜 다른지 알 수 없다
                e["note"] = f"폴더명이 예상({e['expected_folder']})과 다릅니다"
        if folder and folder == rec_folder:
            e["state"] = "recording"
        elif e["exists"]:
            info = ms.derive(folder)
            e["state"] = FOLDER_TO_CAL.get(info["state"], "stt-wait")
            e["folder_label"] = info["label"]           # 정확한 폴더 상태 (칸 보조 문구)
            e["late_start"] = info["late_start"]
            reason = info.get("reason") or ("사람이 제외했습니다" if info["state"] == "excluded" else "")
            # 폴더명 불일치 문구를 덮지 않는다 (둘 다 사람이 알아야 하는 사실이다)
            e["note"] = " · ".join(x for x in (e["note"], reason) if x)
        elif not e["record"]:
            e["state"] = "not-target"
            e["note"] = e.get("skip_reason") or ""
        elif e["start_ts"] and now < e["start_ts"] + grace:
            e["state"] = "planned"
        else:
            # 지난 대상인데 폴더가 없다. v1 제어판은 폴더만 훑어 이 실패가 화면에서 사라졌다
            #  (스펙 3-6 마지막 줄). 캘린더에서만 보인다.
            e["state"] = "missing"
            e["note"] = "예상 폴더가 없습니다"
        e["label"] = CAL_LABEL[e["state"]]
        out.append(e)
    return out


def week_payload(start: str, force: bool = False) -> dict:
    """그 주의 일정 칸.

    **ICS 를 여기서 기다리지 않는다.** 하루 조회가 약 2초라 7일이면 15초가 걸린다
    (실측). 캐시에 있는 날만 바로 담고, 없는 날은 needs_fetch 로 표시해 화면이 하루씩
    채우게 한다. 폴더에서 나오는 정보는 ICS 와 무관하게 항상 담는다 (수용 기준 42).
    """
    days = week_days(start)
    rec = ms.current_recording()
    rec_folder = rec["folder"] if rec else ""
    folders = set(ms.list_folders())
    today = datetime.now(ms.TZ).strftime("%Y-%m-%d")

    out_days = []
    for day in days:
        cached = ms.read_schedule(day)
        ttl = float("inf") if day < today else (60.0 if day == today else 300.0)
        fresh = (not force) and ms.schedule_age(cached) < ttl
        row = {"day": day, "weekday": WEEKDAYS[datetime.strptime(day, "%Y-%m-%d").weekday()],
               "is_today": day == today, "events": [], "error": None,
               "needs_fetch": not fresh, "fetched_at": ""}
        if cached:
            # 캐시가 낡아도 화면에는 먼저 보여준다 (빈 그리드보다 낫다). 갱신은 뒤따라온다.
            row["events"] = decorate_events(cached.get("events") or [], folders, rec_folder, day)
            row["error"] = cached.get("error")
            row["fetched_at"] = cached.get("fetched_at") or ""
        out_days.append(row)

    return {
        "start": days[0], "end": days[-1], "today": today,
        "this_week": days[0] == monday_of(today),
        "days": out_days,
        "orphans": orphan_folders(days, [e for d in out_days for e in d["events"]], rec_folder),
        "cw_error": _cw_error,
    }


def orphan_folders(days: list[str], events: list[dict], rec_folder: str = "") -> list[dict]:
    """그 주 날짜의 회의 폴더 중 일정 칸에 붙지 않은 것.

    ICS 조회가 실패하면 칸이 비는데, 이미 있는 회의는 폴더만으로도 보여야 한다
    (수용 기준 42). 캘린더에서 지운 일정이나 손으로 만든 폴더도 여기로 나온다.
    """
    taken = {e.get("folder") for e in events if e.get("exists")}
    out = []
    for folder in ms.list_folders():
        if folder[:10] not in days or folder in taken:
            continue
        info = ms.derive(folder)
        # 녹음 중은 폴더 상태보다 먼저 본다 (오디오만 있어 폴더로는 글로 옮기는 중로 읽힌다)
        state = "recording" if folder == rec_folder else FOLDER_TO_CAL.get(info["state"], "stt-wait")
        # 배지 색은 확인 필요와 같이 두되(사람이 봐야 하는 것은 맞다) **글자는 폴더 상태를 그대로
        #  적는다.** 회의인지 확인을 「확인 필요」로 적으면 검수할 초안이 있는 줄 알고 열게 된다.
        label = "회의인지 확인" if info["state"] == "suspect" and state != "recording" else CAL_LABEL[state]
        out.append({"folder": folder, "date": info["date"], "title": info["title"],
                    "state": state, "label": label,
                    "folder_label": info["label"], "note": info.get("reason") or ""})
    return out


def event_detail(day: str, folder: str, title: str) -> dict:
    """폴더가 없는 일정을 눌렀을 때 보여줄 것 (스펙 3-6 마지막 문단).

    캘린더 정보와 상태 사유, 그리고 **예상 폴더명**만 보여준다 (수용 기준 38).
    """
    sched = ms.read_schedule(day) or {}
    for ev in sched.get("events") or []:
        if (folder and ev.get("folder") == folder) or (not folder and ev.get("title") == title):
            rec = ms.current_recording()
            got = decorate_events([ev], set(ms.list_folders()), rec["folder"] if rec else "", day)[0]
            got["expected_folder"] = ev.get("folder") or "(시작 시각이 없어 계산할 수 없습니다)"
            got["rel"] = f"docs/meetings/{ev.get('folder')}" if ev.get("folder") else ""
            return got
    return {"error": "그 날짜의 일정 캐시에서 찾지 못했습니다. 다시 조회해 보세요"}


def next_event(events: list[dict]) -> dict | None:
    now = datetime.now(ms.TZ).strftime("%H:%M")
    for ev in events:
        start = ev["when"].split("~")[0]
        if re.match(r"^\d{2}:\d{2}$", start) and start >= now:
            return ev
    return None


def autorecord_on() -> bool:
    return run(["launchctl", "list", RECORDER_LABEL], timeout=10).returncode == 0


def diagnostics() -> dict:
    conf = ms.REPO / ".claude" / "calendar-recorder"
    last = conf / "last-run.txt"
    fails = []
    for m in ms.list_meetings():
        if m["state"] == "failed":
            fails.append({"folder": m["folder"], "reason": m["reason"] or "",
                          "log": m["review"].get("draft", {}).get("log", "")})
    return {
        "last_calendar_check": last.read_text(encoding="utf-8").strip() if last.exists() else "(기록 없음)",
        "launchd_recorder": autorecord_on(),
        "launchd_watcher": run(["launchctl", "list", "com.meeting-console.meeting-console-watcher"],
                               timeout=10).returncode == 0,
        "whisper": bool(shutil.which("mlx_whisper")),
        "diarization_model": (ms.SCRIPTS / "models" / "diarization" / "embedding.onnx").exists(),
        "claude": bool(shutil.which("claude")),
        "ics_url": (ms.REPO / ".claude" / "calendar-recorder" / "ics-url.txt").exists(),
        "recent_failures": fails[:3],
    }


def full_state() -> dict:
    meetings = ms.list_meetings(with_duration=True)
    groups = {label: [] for _, label in ms.STATES}
    for m in meetings:
        row = {k: m[k] for k in ("folder", "date", "title", "state", "label", "reason",
                                 "has_audio", "has_transcript", "has_speakers", "has_draft",
                                 "has_notes", "late_start", "rel", "recording_now")}
        row["duration_min"] = m.get("duration_min")
        row["lock_age"] = (m["lock"] or {}).get("age_sec")
        groups[m["label"]].append(row)
    # 확정은 최근 10건만 (스펙 5-2)
    groups["확정"] = groups["확정"][:10]
    today = today_schedule()
    rec = ms.current_recording()
    if rec:
        rec["title"] = ms.meeting_title(rec["folder"], ms.MEETINGS / rec["folder"])
    return {
        "now": datetime.now(ms.TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "recording": rec,
        "autorecord": autorecord_on(),
        "today": today,
        "next_event": next_event(today["events"]),
        "groups": groups,
        # 사람이 할 일은 「확인 필요」뿐이라 따로 떼어 맨 위에 둔다 (스펙 8절 「검수 큐 표시 방식」).
        #  나머지는 그 아래 「처리 상태」 표로 간다. 「검수 큐」라는 이름이 "내가 검수할 것"으로
        #  읽혀 확인 필요 0건인데 표에 6건이 떠 있는 혼란이 실사용에서 확인됐다 (2026-09-02).
        "review_labels": ["확인 필요"],
        "status_order": [label for _, label in ms.STATES if label != "확인 필요"],
        "order": ["확인 필요"] + [label for _, label in ms.STATES if label != "확인 필요"],
        # 배지는 **확인 필요만** 센다 (스펙 8절 3단계 결정, 2026-09-04). 회의인지 확인은
        #  「처리 상태」 표로 내려갔으므로 이 값과 「확인 필요」 절의 행 수, 메뉴바의
        #  「확인 필요 N건」이 항상 같아야 한다.
        "waiting": len(groups["확인 필요"]),
        "diagnostics": diagnostics(),
        "enroll": {k: {"state": v.get("state", ""), "message": v.get("message", "")}
                   for k, v in _enroll.items()},
    }


# ---------------------------------------------------------------- 검수 화면

def meeting_detail(folder: str) -> dict:
    d = ms.MEETINGS / folder
    info = ms.derive(folder)
    draft = (d / ms.DRAFT_NAME)
    notes = (d / ms.NOTES_NAME)
    log_tail = ""
    log_rel = info["review"].get("draft", {}).get("log", "")
    if log_rel:
        p = ms.REPO / log_rel
        if p.exists():
            log_tail = "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])

    # 화자 등록으로 분리본이 갱신되면 초안은 그대로다. 그 사실을 검수 화면에 띄운다 (스펙 5-3 7번).
    spk_file = d / "transcript-speakers.md"
    hint = ""
    if spk_file.exists() and draft.exists() and spk_file.stat().st_mtime > draft.stat().st_mtime:
        hint = "화자 분리본이 초안보다 최근입니다 (화자 등록 등으로 갱신됨). 초안 다시 생성을 권합니다."
    return {
        **{k: info[k] for k in ("folder", "date", "title", "state", "label", "reason",
                                "has_speakers", "late_start", "has_draft", "has_notes")},
        "draft_text": draft.read_text(encoding="utf-8", errors="replace") if draft.exists() else "",
        "notes_text": notes.read_text(encoding="utf-8", errors="replace") if notes.exists() else "",
        "speakers": (d / "transcript-speakers.md").read_text(encoding="utf-8", errors="replace")
                    if (d / "transcript-speakers.md").exists() else "",
        "attendees": (d / "attendees.md").read_text(encoding="utf-8", errors="replace")
                     if (d / "attendees.md").exists() else "",
        "late_note": (d / "late-start.txt").read_text(encoding="utf-8", errors="replace")
                     if (d / "late-start.txt").exists() else "",
        "index_row": info["review"].get("index_row", {}),
        "speaker_hint": hint,
        "enroll": _enroll.get(folder, {"state": "idle", "message": ""}),
        "log_path": log_rel,
        "log_tail": log_tail,
        "review": info["review"],
    }


def search_transcript(folder: str, q: str) -> list[dict]:
    p = ms.MEETINGS / folder / "transcript.md"
    if not p.exists() or not q:
        return []
    hits = []
    for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if q in line:
            hits.append({"line": i, "text": line.strip()})
        if len(hits) >= 60:
            break
    return hits


# ---------------------------------------------------------------- 확정 · 제외

INDEX_COLS = ("종류", "주제", "관련 프로젝트", "핵심 결정")

# 인덱스 표의 머리. 번들(`make-bundle.sh`)이 넣는 빈 README 도 같은 두 줄이다.
#  한쪽만 바꾸면 새 설치자의 첫 확정이 "인덱스 표를 찾지 못했다"로 끝나므로 문구를 맞춘다.
INDEX_HEADER = ("| 날짜 | 폴더 | 종류 | 주제 | 관련 프로젝트 | 핵심 결정 |\n"
                "|---|---|---|---|---|---|\n")


def cell(v: str) -> str:
    return (v or "").replace("|", "/").replace("\n", " ").strip() or "-"


def add_index_row(folder: str, row: dict) -> str:
    """docs/meetings/README.md 인덱스 표 맨 위에 한 줄 넣는다. 기존 줄은 건드리지 않는다.

    파일이 없으면 표 머리만 든 것을 새로 만든다. 새로 설치한 사람은 회의 폴더가 하나도
    없어 README 도 없는데, 예전에는 여기서 `FileNotFoundError` 가 나 첫 확정이 500 이 됐다
    (그때 `notes.md` 는 이미 써진 뒤라 반쪽으로 남았다).
    """
    path = ms.INDEX_MD
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = INDEX_HEADER
        path.write_text(text, encoding="utf-8")
    if f"({folder}/notes.md)" in text:
        return "이미 인덱스에 있어 추가하지 않았습니다"
    date = folder[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", folder) else datetime.now(ms.TZ).strftime("%Y-%m-%d")
    line = (f"| {date} | [{folder}]({folder}/notes.md) | {cell(row.get('종류'))} | "
            f"{cell(row.get('주제'))} | {cell(row.get('관련 프로젝트'))} | {cell(row.get('핵심 결정'))} |")
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("| 날짜 | 폴더 |") and i + 1 < len(lines) and set(lines[i + 1]) <= set("|- "):
            lines.insert(i + 2, line)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return "인덱스에 1행 추가"
    return "인덱스 표를 찾지 못해 추가하지 못했습니다"


DRAFT_BANNER = "> ⚠️ 자동 생성 초안"


def approve(folder: str, text: str, row: dict) -> dict:
    """초안을 확정본으로 굳히고 인덱스에 한 줄 넣는다.

    **인덱스 추가가 실패해도 확정 자체는 성공으로 끝낸다.** 확정의 본체는 `notes.md` 와
    `review.json` 이고 그 둘은 이미 써진 뒤다. 예전에는 인덱스에서 예외가 나면 응답 없이
    끝나 `notes.md` 만 있고 판정이 없는 반쪽 상태가 남았고, 사람은 "이미 확정된 노트가
    있습니다" 만 보고 되돌릴 방법이 없었다. 실패는 메시지로 알리고 사람이 손으로 고친다.
    """
    d = ms.MEETINGS / folder
    if not isinstance(row, dict):
        # 인덱스 행이 dict 가 아니면 아무것도 쓰기 전에 거절한다. 쓰고 나서 터지면 반쪽이 된다.
        return {"ok": False, "error": "인덱스 행 형식이 올바르지 않습니다", "status": 400}
    if (d / ms.NOTES_NAME).exists():
        return {"ok": False, "error": "이미 확정된 노트가 있습니다"}
    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "노트 내용이 비어 있습니다"}
    lines = [ln for ln in body.splitlines()]
    while lines and (lines[0].startswith(DRAFT_BANNER) or not lines[0].strip()):
        lines.pop(0)          # 초안 표시 줄은 확정본에 남기지 않는다
    (d / ms.NOTES_NAME).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    ms.write_review(folder, {"status": "approved", "reason": "사람이 확정",
                             "decided_at": ms.now_iso(),
                             "index_row": {k: cell(row.get(k)) for k in INDEX_COLS}})
    try:
        msg = add_index_row(folder, row)
    except Exception as exc:                            # noqa: BLE001
        return {"ok": True, "message": f"확정됨. 인덱스 추가 실패: {safe_reason(exc)}"}
    return {"ok": True, "message": f"notes.md 생성, {msg}"}


DELETED_DIR = ms.STATE_DIR / "deleted"


def exclude(folder: str, reason: str, delete: bool, confirm: str) -> dict:
    if delete:
        # 오디오 삭제는 되돌릴 수 없다. 폴더명을 정확히 타이핑해야만 실행한다 (스펙 3-4 넷째 겹)
        if confirm != folder:
            return {"ok": False, "error": "폴더명이 정확히 일치하지 않아 삭제하지 않았습니다"}
        target = ms.MEETINGS / folder
        # 삭제 이력은 폴더 밖에 남긴다. review.json 을 폴더 안에만 쓰면 방금 쓴 판정까지
        #  같이 지워져 "무엇을 왜 지웠는지"가 아무 데도 남지 않는다.
        record = {"folder": folder, "status": "excluded", "reason": reason or "사람이 제외",
                  "decided_at": ms.now_iso(), "deleted": True,
                  "path": f"docs/meetings/{folder}",
                  "files": sorted(p.name for p in target.iterdir()) if target.is_dir() else [],
                  "review_before_delete": ms.read_review(folder)}
        ms.write_json(DELETED_DIR / f"{folder}.json", record)
        shutil.rmtree(target)
        rel = (DELETED_DIR / f"{folder}.json").relative_to(ms.CONSOLE)
        return {"ok": True, "message": f"{folder} 폴더를 삭제했습니다 (되돌릴 수 없습니다)."
                                       f" 삭제 이력: meeting-console/{rel}"}
    ms.write_review(folder, {"status": "excluded", "reason": reason or "사람이 제외",
                             "decided_at": ms.now_iso()})
    return {"ok": True, "message": "제외로 표시했습니다 (폴더는 그대로 둡니다)"}


def regenerate(folder: str) -> dict:
    d = ms.MEETINGS / folder
    if ms.lock_info(folder):
        return {"ok": False, "error": "이미 초안을 만들고 있습니다"}
    (d / ms.DRAFT_NAME).unlink(missing_ok=True)
    log = (ms.LOGS / f"regen-{folder}.log").open("a", encoding="utf-8")
    subprocess.Popen([UV, "run", "--quiet", str(ms.CONSOLE / "watcher.py"),
                      "--once", "--folder", folder, "--force"],
                     cwd=str(ms.REPO), stdout=log, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "message": "다시 생성을 시작했습니다 (수 분 걸립니다)"}


# ---------------------------------------------------------------- 녹음 제어

def stop_recording() -> dict:
    """kill -INT 로 보내고 종료를 기다린다.

    ⚠️ SIGKILL 을 쓰지 않는다. 강제 종료하면 색인(moov)이 안 쓰여 오디오 데이터가 남아도
       파일을 열 수 없다 (2026-08-20 실제 사고).
    """
    rec = ms.current_recording()
    if not rec:
        return {"ok": False, "error": "녹음 중이 아닙니다"}
    try:
        os.kill(rec["pid"], signal.SIGINT)
    except ProcessLookupError:
        return {"ok": False, "error": "이미 끝났습니다"}
    for _ in range(40):                      # 최대 20초 기다린다
        time.sleep(0.5)
        if ms.current_recording() is None:
            return {"ok": True, "message": f"{rec['folder']} 녹음을 종료했습니다"}
    return {"ok": True, "message": "종료 신호를 보냈고 아직 정리 중입니다 (강제 종료는 하지 않습니다)"}


def set_autorecord(on: bool) -> dict:
    uid = os.getuid()
    if on:
        if not RECORDER_PLIST.exists():
            return {"ok": False, "error": f"plist 가 없습니다: {RECORDER_PLIST}"}
        r = run(["launchctl", "bootstrap", f"gui/{uid}", str(RECORDER_PLIST)])
    else:
        r = run(["launchctl", "bootout", f"gui/{uid}/{RECORDER_LABEL}"])
    ok = autorecord_on() == on
    return {"ok": ok, "message": f"자동 녹음 {'켬' if on else '끔'}",
            "error": None if ok else (r.stderr or r.stdout or "").strip()}


# ---------------------------------------------------------------- 화자 등록

BLOCK_RE = re.compile(r"^\*\*\[(\d+):(\d+)\]\s+(.+?)\*\*:\s*(.*)$")
TALK_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*[\d.]+분\s*\|\s*[\d.]+%\s*\|$")
SPEAKER_RE = re.compile(r"^SPEAKER_(\d+)$")
UNKNOWN_LABEL = "(불명)"      # diarize.py 가 화자를 못 가린 블록에 붙이는 라벨


def talk_rows(text: str) -> list[str]:
    """분리본 발화량 표의 라벨을 표에 적힌 순서대로. 행 수가 곧 화자(클러스터) 수다."""
    rows = []
    for line in text.splitlines():
        m = TALK_ROW_RE.match(line.strip())
        if not m:
            continue
        label = m.group(1).strip()
        if label in ("화자", "---"):
            continue
        rows.append(label)
    return rows


def label_indices(text: str) -> dict[str, int | None]:
    """분리본의 발화량 표에서 라벨 -> 화자 번호를 뽑는다. 못 정하면 None.

    diarize.py 는 `--enroll "SPEAKER_00=이름"` 처럼 **번호로만** 받는다. 이름이 붙은 라벨을
    고치려면 그 라벨의 번호를 알아야 하는데 분리본 본문에는 번호가 없다. 표는
    `sorted(talk.items())` 로 찍히므로 행 순서가 화자 번호 순서다. 그 순서로 번호를 되찾는다.

    번호가 드러난 행(SPEAKER_NN)의 위치가 번호와 어긋나면 순서 추정을 믿지 않고,
    이름 붙은 라벨은 None 으로 둔다. 엉뚱한 화자를 그 이름으로 등록하면 등록부가 오염된다.
    """
    rows = talk_rows(text)
    explicit = {i: int(m.group(1)) for i, lb in enumerate(rows)
                if (m := SPEAKER_RE.match(lb))}
    ordered = all(pos == idx for pos, idx in explicit.items())
    out: dict[str, int | None] = {}
    for pos, label in enumerate(rows):
        if rows.count(label) > 1:              # 같은 이름이 두 화자에 붙어 있으면 못 가린다
            out[label] = None
        elif pos in explicit:
            out[label] = explicit[pos]
        else:
            out[label] = pos if ordered else None
    return out


def dup_counts(text: str) -> dict[str, int]:
    """분리본 발화량 표에서 라벨 -> 그 이름이 붙은 클러스터 수.

    같은 이름이 두 화자에 붙으면(오인식 또는 사람이 둘에 같은 이름을 붙인 경우) 라벨만 보고는
    구분할 수 없다. 화면에서 하나로 합쳐 보여주면 사람이 "왜 항목이 하나뿐인가"를 모른 채
    영구히 잠긴다. 개수를 드러내고 재분리로 유도하는 것이 v2 가 메우려던 시나리오다.
    """
    out: dict[str, int] = {}
    for lb in talk_rows(text):
        out[lb] = out.get(lb, 0) + 1
    return out


def shown(label: str, dup: dict[str, int]) -> str:
    """화면·힌트에 적을 이름. 중복이면 클러스터 수를 붙여 드러낸다."""
    n = dup.get(label, 1)
    return f"{label} ({n}개 클러스터)" if n > 1 else label


DUP_REASON = ("같은 이름이 두 화자에 붙었습니다. 어느 쪽이 이 사람인지 분리본만으로 가릴 수 없으니,"
              " 아래 화자 수 확인 줄에 실제 인원을 넣어 다시 분리하세요")


def speaker_segments(folder: str) -> dict:
    """화자 분리본에서 라벨마다 대표 구간 3개를 뽑아 클립으로 자른다 (스펙 5-3).

    시작 시각은 분리본에서, 끝은 다음 블록 시작으로 잡는다.
    1초 미만 구간은 맞장구일 가능성이 커서 제외한다 (diarize.py 의 임베딩 추출과 같은 기준).
    """
    d = ms.MEETINGS / folder
    spk = d / "transcript-speakers.md"
    if not spk.exists():
        return {"ok": False, "error": "화자 분리본이 없습니다"}
    audios = ms.audio_files(d)
    if not audios:
        return {"ok": False, "error": "오디오가 없습니다"}
    audio = audios[0]

    spk_text = spk.read_text(encoding="utf-8", errors="replace")
    idx_of = label_indices(spk_text)
    dup = dup_counts(spk_text)
    blocks = []
    for line in spk_text.splitlines():
        m = BLOCK_RE.match(line.strip())
        if m:
            mm, ss, label, text = m.groups()
            blocks.append({"start": int(mm) * 60 + int(ss), "label": label.strip(), "text": text})
    total = ms.audio_seconds(audio) or (blocks[-1]["start"] + 30 if blocks else 0)
    for i, b in enumerate(blocks):
        b["end"] = blocks[i + 1]["start"] if i + 1 < len(blocks) else total

    by_label: dict[str, list] = {}
    for b in blocks:
        if b["end"] - b["start"] < 1.0:
            continue
        by_label.setdefault(b["label"], []).append(b)

    out_dir = ms.CLIP_DIR / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    # ⚠️ 클립 파일명에 분리본 mtime 을 넣는다. 파일 존재만 보고 재사용하면 화자 등록으로 분리본이
    #    갱신된 뒤에도 옛 클립을 준다. 화면 라벨과 다른 목소리를 듣고 이름을 붙이면 잘못된
    #    임베딩이 등록부에 들어가 **이후 모든 회의의 자동 인식이 오염된다.**
    stamp = int(spk.stat().st_mtime)
    for old in out_dir.glob("*.m4a"):
        if not old.name.startswith(f"{stamp}-"):
            old.unlink(missing_ok=True)        # 옛 분리본에서 자른 클립은 남기지 않는다
    labels = []
    for idx, (label, segs) in enumerate(by_label.items()):
        picks = sorted(segs, key=lambda x: x["end"] - x["start"], reverse=True)[:3]
        picks.sort(key=lambda x: x["start"])
        items = []
        for n, seg in enumerate(picks, 1):
            dur = min(seg["end"] - seg["start"], CLIP_MAX_SEC)
            # 파일명은 라벨 순번으로 만든다. 한글 라벨을 그대로 치환하면
            #  두 사람 이름이 모두 "___"가 되어 클립이 서로 덮어쓴다.
            clip = out_dir / f"{stamp}-s{idx}-{n}.m4a"
            if not clip.exists():
                run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(seg["start"]),
                     "-t", f"{dur:.2f}", "-i", str(audio), "-c:a", "aac", "-b:a", "64k",
                     str(clip)], timeout=90)
            items.append({
                "url": f"/clips/{folder}/{clip.name}",
                "start": seg["start"], "seconds": round(dur, 1),
                "at": f"{seg['start'] // 60:02d}:{seg['start'] % 60:02d}",
                "text": seg["text"][:160],
            })
        # 이름이 붙은 라벨도 고칠 수 있게 둔다 (스펙 5-3 4번). 코사인 임계값 0.5 로 붙은 이름은
        #  틀릴 수 있다 (실측: 유사도 0.51 로 오인식). 화면에서는 잠긴 상태로 두고 사람이
        #  "이름 수정"을 눌러야 입력칸이 열린다.
        spk_idx = idx_of.get(label) if label != UNKNOWN_LABEL else None
        if spk_idx is not None:
            reason = ""
        elif label == UNKNOWN_LABEL:
            reason = "겹쳐 말하거나 짧은 맞장구라 화자를 특정하지 못한 구간입니다 (등록 대상이 아닙니다)"
        elif dup.get(label, 1) > 1:
            reason = DUP_REASON
        else:
            reason = "이 라벨의 화자 번호를 분리본에서 확정할 수 없어 콘솔에서 고칠 수 없습니다"
        labels.append({"label": label, "seconds": round(sum(s["end"] - s["start"] for s in segs)),
                       "clips": items,
                       "named": not SPEAKER_RE.match(label) and label != UNKNOWN_LABEL,
                       "speaker_index": spk_idx,
                       "enrollable": spk_idx is not None,
                       "clusters": dup.get(label, 1),
                       "shown": shown(label, dup),
                       "locked_reason": reason,
                       "duplicate": dup.get(label, 1) > 1})
    labels.sort(key=lambda x: -x["seconds"])
    return {"ok": True, "folder": folder, "labels": labels,
            "registry": sorted(ms.read_json(ms.VOICES, {}) or {}),
            "has_draft": (d / ms.DRAFT_NAME).exists(),
            **speaker_count_info(folder, spk_text)}


def speaker_count_info(folder: str, spk_text: str) -> dict:
    """화자 수 확인 줄에 쓸 재료 (스펙 3-5, 수용 기준 36).

    판정은 하지 않는다. 분리 결과 인원 · 발화량 1분 미만 라벨 · 참석자 수락 인원을 한 줄에
    모아주는 것까지가 콘솔 몫이고, 몇 명이었는지는 그 자리에 있던 사람만 안다.
    권유 조건은 두 가지 중 하나다 (스펙 8절 결정 3): 수락 인원과 분리 결과가 다르거나,
    발화량 1분 미만 화자가 있을 때.
    """
    mins = talk_minutes(spk_text)
    rows = [lb for lb in talk_rows(spk_text) if lb != UNKNOWN_LABEL]
    dup = dup_counts(spk_text)
    # 중복 라벨은 표에 두 행이므로 한 번만 적는다. 힌트에 같은 이름이 두 번 나오면 오타로 읽힌다.
    low, done = [], set()
    for lb in rows:
        if mins.get(lb, 0.0) >= 1.0 or lb in done:
            continue
        done.add(lb)
        low.append({"label": lb, "shown": shown(lb, dup), "minutes": mins.get(lb, 0.0)})
    accepted = accepted_count(folder)
    dups = [lb for lb, n in dup.items() if n > 1 and lb != UNKNOWN_LABEL]
    reasons = []
    if dups:
        reasons.append("같은 이름이 두 화자에 붙어 있습니다 ("
                       + ", ".join(shown(lb, dup) for lb in dups)
                       + "). 실제 인원을 넣어 다시 분리하면 풀립니다")
    if accepted is not None and accepted != len(rows):
        reasons.append(f"참석 수락 {accepted}명과 분리 결과 {len(rows)}명이 다릅니다")
    if low:
        reasons.append("발화량이 1분이 안 되는 화자가 있습니다 ("
                       + ", ".join(f"{x['shown']} {x['minutes']}분" for x in low) + ")")
    return {"speaker_count": len(rows), "low_talkers": low, "accepted": accepted,
            "duplicates": [shown(lb, dup) for lb in dups],
            "suggest_resplit": bool(reasons), "resplit_reasons": reasons}


def talk_minutes(text: str) -> dict[str, float]:
    """분리본 발화량 표에서 라벨 -> 발화 분. 화자 수 확인 줄이 쓴다 (스펙 3-5)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        m = re.match(r"^\|\s*([^|]+?)\s*\|\s*([\d.]+)분\s*\|\s*[\d.]+%\s*\|$", line.strip())
        if m and m.group(1).strip() not in ("화자", "---"):
            out[m.group(1).strip()] = float(m.group(2))
    return out


def accepted_count(folder: str) -> int | None:
    """참석자 기록의 ACCEPTED 행 수. 초대 명단이라 실제 참석과 다를 수 있다 (스펙 3-5)."""
    p = ms.MEETINGS / folder / "attendees.md"
    if not p.exists():
        return None
    return sum(1 for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
               if ln.strip().startswith("| ACCEPTED"))


# ---- 「등록부에 저장 끔」 이름의 기억 (수용 기준 30)
#
# 저장 끔은 diarize.py 를 부르지 않고 분리본·초안의 라벨 문자열만 치환한다. 그래서 그 뒤에
# 다른 화자를 등록(저장 켬)하거나 재분리하면 diarize.py 가 분리본을 새로 써서 **그 이름이 예고
# 없이 사라진다.** 초안에는 남아 있어 둘이 어긋난다.
#
# 서랍에 경고만 띄우는 길도 있었지만(선택 b), 경고는 사람이 그 화자를 다시 붙이는 일을 대신해
# 주지 않는다. 그래서 이름을 review.json 에 기억해 두고 분리가 끝난 뒤 다시 붙인다(선택 a).
# review.json 을 쓰는 이유는 회의 폴더와 같이 움직이기 때문이다. `.state` 는 지워도 되는 곳이라
# 사람이 붙인 이름을 거기 두면 안 된다.
#
# **클러스터 수가 달라지면 다시 붙이지 않는다.** SPEAKER_00 이 같은 사람이라는 보장이 없어서다
# (등록부 오염을 막는 것과 같은 이유). 이때는 사라졌다는 사실을 화면 메시지로 알린다.
LOCAL_KEY = "local_speaker_names"
LOCAL_N_KEY = "local_speaker_clusters"


def remember_local(folder: str, idx_names: dict[int, str], clusters: int) -> None:
    cur = ms.read_review(folder)
    keep = dict(cur.get(LOCAL_KEY) or {}) if cur.get(LOCAL_N_KEY) == clusters else {}
    keep.update({f"SPEAKER_{i:02d}": n for i, n in idx_names.items()})
    ms.write_review(folder, {LOCAL_KEY: keep, LOCAL_N_KEY: clusters})


def reapply_local(folder: str, spk_out: Path, draft: Path) -> str:
    """분리가 끝난 뒤 저장 끔 이름을 다시 붙인다. 화면에 덧붙일 문장을 돌려준다."""
    cur = ms.read_review(folder)
    saved = dict(cur.get(LOCAL_KEY) or {})
    if not saved or not spk_out.exists():
        return ""
    text = spk_out.read_text(encoding="utf-8", errors="replace")
    now_n = cluster_count(text)
    names = ", ".join(saved.values())
    if cur.get(LOCAL_N_KEY) != now_n:
        ms.write_review(folder, {LOCAL_KEY: {}, LOCAL_N_KEY: now_n})
        return (f" 등록부에 저장하지 않았던 이름({names})은 화자 수가 달라져 다시 붙이지 않았습니다."
                " 같은 화자인지 보장할 수 없습니다. 구간을 듣고 다시 붙이세요.")
    live = {lb: p for lb, p in saved.items() if lb in set(talk_rows(text))}
    if not live:
        return ""
    with ms.path_lock(spk_out):
        replace_labels(spk_out, live)
    replace_labels(draft, live)
    return f" 등록부에 저장하지 않았던 이름({', '.join(live.values())})을 다시 붙였습니다."


def replace_labels(path: Path, mapping: dict[str, str]) -> int:
    """파일 안의 라벨 문자열만 이름으로 바꾼다. 바뀐 곳 수를 돌려준다.

    **초안을 다시 만들지 않는다** (스펙 3-3, 수용 기준 31). 사람이 이미 손으로 고쳐둔 초안이
    날아가면 검수를 처음부터 다시 해야 한다. SPEAKER_00 같은 라벨만 치환하므로 초안이 발화자를
    라벨 없이 서술한 부분은 그대로 남는다 ("초안 다시 생성"을 함께 권하는 이유다).
    """
    if not path.exists() or not mapping:
        return 0
    text = orig = path.read_text(encoding="utf-8", errors="replace")
    hits = 0
    for label, person in mapping.items():
        if not label or not person or label == person:
            continue
        # 라벨 경계를 본다. SPEAKER_0 이 SPEAKER_01 의 앞부분을 갉아먹지 않게.
        pat = re.compile(re.escape(label) + r"(?![0-9A-Za-z_])")
        text, n = pat.subn(person, text)
        hits += n
    if hits and text != orig:
        path.write_text(text, encoding="utf-8")
    return hits


def unnamed_labels(text: str) -> list[str]:
    """분리본 발화량 표에서 아직 SPEAKER_NN 인 라벨."""
    return [lb for lb in talk_rows(text) if SPEAKER_RE.match(lb)]


def diarize_cmd(audio: Path, vtt: Path, out: Path, n_speakers: int, enroll_arg: str = "") -> list[str]:
    """diarize.py 호출 한 줄. 등록부 경로는 ms.DIARIZE 가 어디를 가리키느냐로 갈린다."""
    cmd = [UV, "run", "--quiet", str(ms.DIARIZE), str(audio), str(vtt), str(out)]
    if enroll_arg:
        cmd += ["--enroll", enroll_arg]
    if n_speakers >= 1:
        cmd += ["--speakers", str(n_speakers)]
    return cmd


def cluster_count(spk_text: str) -> int:
    """지금 분리본의 화자(클러스터) 수. 다시 돌릴 때 같은 수를 넘겨야 라벨이 어긋나지 않는다."""
    n = len([lb for lb in talk_rows(spk_text) if lb != UNKNOWN_LABEL])
    if n >= 1:
        return n
    labels = {m.group(3).strip() for m in
              (BLOCK_RE.match(ln.strip()) for ln in spk_text.splitlines()) if m}
    return len([x for x in labels if x != UNKNOWN_LABEL])


def enroll(folder: str, names: dict, save_to_registry: bool = True) -> dict:
    """검수 화면 서랍에서 부른다. 라벨에 이름을 붙이고, 켜져 있으면 등록부에도 저장한다.

    **등록부에 저장할 때만 diarize.py 를 두 번 돌린다.** diarize.py 는 `--enroll` 실행에서
    등록부 대조를 건너뛴다 (배타 분기). 그래서 한 명을 등록하면 이미 이름이 붙어 있던 다른
    화자가 SPEAKER_NN 으로 돌아가고, 그 화자를 등록하면 이번엔 앞사람 이름이 떨어진다.
    `--enroll` 없이 한 번 더 부르면 등록부 대조가 돌아 전원 이름이 붙는다 (실측 유사도 1.00).
    두 호출 모두 같은 `--speakers N` 을 넘겨 클러스터를 같게 유지한다. scripts/ 는 고치지 않는다.

    **등록부 저장을 끄면 diarize.py 를 부르지 않는다.** `--enroll` 은 등록부 쓰기와 한 몸이라
    끄고 부를 방법이 없다. 대신 분리본과 초안의 라벨 문자열을 치환한다. 이 회의에만 이름이
    붙고 다음 회의에는 영향이 없다 (스펙 3-3 의 의도가 그것이다). 수 분을 기다리지 않는다.
    """
    d = ms.MEETINGS / folder
    audios = ms.audio_files(d)
    vtt = d / "transcript.vtt"
    spk_out = d / "transcript-speakers.md"
    draft = d / ms.DRAFT_NAME
    if not spk_out.exists():
        return {"ok": False, "error": "화자 분리본이 없어 등록할 수 없습니다"}
    if _enroll.get(folder, {}).get("state") == "running":
        return {"ok": False, "error": "이미 분리 작업을 진행 중입니다"}

    spk_text = spk_out.read_text(encoding="utf-8", errors="replace")
    idx_of = label_indices(spk_text)
    pairs, mapping, bad = [], {}, []
    for label, value in names.items():
        person = (value or "").strip()
        if not person or label not in idx_of:
            continue
        idx = idx_of.get(label)
        if idx is None:
            bad.append(label)
            continue
        pairs.append(f"SPEAKER_{idx:02d}={person}")
        mapping[label] = person
    if bad:
        dup = dup_counts(spk_text)
        if any(dup.get(lb, 1) > 1 for lb in bad):
            return {"ok": False, "error": f"{', '.join(shown(lb, dup) for lb in bad)}: {DUP_REASON}"}
        return {"ok": False,
                "error": f"화자 번호를 확정할 수 없는 라벨이 있어 등록하지 않았습니다: {', '.join(bad)}"}
    if not mapping:
        return {"ok": False, "error": "등록할 이름이 없습니다"}
    names_txt = ", ".join(mapping.values())

    # ---- 등록부에 저장하지 않는 경우: 라벨 치환만. diarize.py 를 부르지 않는다
    if not save_to_registry:
        with ms.path_lock(spk_out):
            n_spk = replace_labels(spk_out, mapping)
        n_draft = replace_labels(draft, mapping)
        # 이 회의 안에서 다시 분리가 돌면 이 이름이 사라지므로 폴더에 기억해 둔다
        remember_local(folder, {idx_of[lb]: p for lb, p in mapping.items()
                                if idx_of.get(lb) is not None},
                       cluster_count(spk_text))
        return {"ok": True,
                "message": f"이름을 붙였습니다: {names_txt} (등록부에 저장하지 않음). "
                           f"분리본 {n_spk}곳 · 초안 {n_draft}곳을 바꿨습니다. "
                           f"다음 회의에서는 자동으로 붙지 않습니다. "
                           f"이 회의에서 다시 분리를 돌리면 이 이름을 자동으로 다시 붙입니다"}

    if not audios or not vtt.exists():
        return {"ok": False, "error": "오디오 또는 transcript.vtt 가 없어 등록부에 저장할 수 없습니다"}

    log_file = ms.LOGS / f"enroll-{folder}.log"
    # 화자 수를 넘겨준다. 안 넘기면 diarize.py 가 임계값으로 다시 추정하는데, 그 결과가 지금
    #  화면에 보이는 분리본과 달라지면 SPEAKER_00 이 사람이 들은 그 화자가 아니게 된다.
    #  (실측: 27분 1on1 을 자동 추정으로 다시 돌렸더니 화자가 150명으로 갈렸다.)
    n_speakers = cluster_count(spk_text)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"{folder}-transcript-speakers.md"
    shutil.copy2(spk_out, backup)      # 1차 실행 전에 남긴다. 도중에 서버가 꺼져도 되돌릴 것이 있다
    set_job(folder, kind="등록", state="running", started=ms.now_iso(), stage=1, stages=2,
            stage_label="1/2 목소리를 등록부에 저장하며 다시 분리하는 중",
            message="화자 분리를 다시 돌리는 중입니다 (수 분). 이 화면을 닫아도 계속됩니다")

    def worker():
        try:
            with log_file.open("w", encoding="utf-8") as fh:
                fh.write(f"# 화자 등록 | {folder} | {ms.now_iso()}\n"
                         f"# 1차: --enroll 로 등록부에 목소리를 저장한다\n")
                code1 = run_diarize(fh, audios[0], vtt, spk_out, n_speakers, ",".join(pairs))
                if code1 != 0:
                    shutil.copy2(backup, spk_out)      # 1차가 깨졌으면 손대기 전으로
                    backup.unlink(missing_ok=True)
                    set_job(folder, state="failed",
                            message=f"등록 실패 (종료 코드 {code1}). 분리본은 그대로 두었습니다."
                                    f" 로그: {log_file.name}")
                    return

                # 1차 결과를 잃지 않게 백업을 1차 결과로 갱신하고 2차를 돌린다. 2차가 깨지면 되돌린다.
                shutil.copy2(spk_out, backup)
                fh.write("\n# 2차: --enroll 없이 다시 돌려 등록부 대조로 전원 이름을 붙인다\n")
                set_job(folder, stage=2, stage_label="2/2 등록부와 대조해 전원에게 이름을 붙이는 중")
                try:
                    code2 = run_diarize(fh, audios[0], vtt, spk_out, n_speakers)
                except subprocess.TimeoutExpired:
                    code2 = -1
                    fh.write("\n[server] 2차 호출이 제한 시간을 넘겨 끊었습니다\n")
                good2 = code2 == 0 and spk_out.exists() and spk_out.stat().st_size > 0
                if not good2:
                    shutil.copy2(backup, spk_out)      # 1차 분리본으로 되돌린다
                    fh.write("\n[server] 2차 실패. 1차 분리본으로 되돌렸습니다\n")
                backup.unlink(missing_ok=True)

            # 초안은 다시 만들지 않고 라벨 문자열만 바꾼다 (수용 기준 31)
            n_draft = replace_labels(draft, mapping)
            draft_hint = ""
            if draft.exists():
                draft_hint = (f" 초안의 라벨 {n_draft}곳을 이름으로 바꿨습니다"
                              " (초안을 다시 만들지 않았습니다). 발화자를 라벨 없이 서술한 부분까지"
                              " 고치려면 '다시 생성'을 누르세요.")
            local_hint = reapply_local(folder, spk_out, draft)
            if good2:
                left = unnamed_labels(spk_out.read_text(encoding="utf-8", errors="replace"))
                rest = (f" 아직 이름이 없는 화자 {len(left)}명({', '.join(left)})은 구간을 듣고 이어서 등록하세요."
                        if left else " 분리본의 모든 화자에 이름이 붙었습니다.")
                msg = f"등록 완료: {names_txt}.{rest}{local_hint}{draft_hint}"
            else:
                msg = (f"등록은 됐습니다: {names_txt}. 다만 등록부 대조 재실행이 실패해"
                       f" (종료 코드 {code2}) 1차 분리본으로 되돌렸고 다른 화자 이름이"
                       f" SPEAKER_NN 으로 남아 있을 수 있습니다."
                       f" 로그: {log_file.name}. 같은 화면에서 한 번 더 저장하면 다시 시도합니다."
                       + local_hint + draft_hint)
            set_job(folder, state="done", message=msg)
        except Exception as exc:                   # noqa: BLE001
            if backup.exists():
                shutil.copy2(backup, spk_out)
                backup.unlink(missing_ok=True)
            set_job(folder, state="failed", message=f"{type(exc).__name__}: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "message": "등록을 시작했습니다. 분리를 두 번 돌리므로 수 분 걸립니다"}


def run_diarize(fh, audio: Path, vtt: Path, out: Path, n_speakers: int, enroll_arg: str = "") -> int:
    cmd = diarize_cmd(audio, vtt, out, n_speakers, enroll_arg)
    fh.write(f"\n$ {' '.join(cmd)}\n")
    fh.flush()
    return subprocess.run(cmd, cwd=str(ms.REPO), stdout=fh,
                          stderr=subprocess.STDOUT, timeout=3600).returncode


def resplit(folder: str, n: int) -> dict:
    """사람이 넣은 화자 수로 다시 분리한다 (스펙 3-5, 수용 기준 36).

    `--enroll` 없이 한 번만 부른다. 등록부 대조가 돌아 아는 목소리에는 이름이 다시 붙는다.
    클러스터가 달라지면 대조 결과도 달라져 이름을 잃을 수 있으므로 실행 전에 화면에서 알린다.
    실패하면 이전 분리본으로 되돌린다.
    """
    d = ms.MEETINGS / folder
    audios = ms.audio_files(d)
    vtt = d / "transcript.vtt"
    spk_out = d / "transcript-speakers.md"
    if not (1 <= n <= 20):
        return {"ok": False, "error": "화자 수는 1~20 사이로 넣어주세요"}
    if not audios or not vtt.exists():
        return {"ok": False, "error": "오디오 또는 transcript.vtt 가 없어 다시 분리할 수 없습니다"}
    if not spk_out.exists():
        return {"ok": False, "error": "화자 분리본이 없습니다"}
    if _enroll.get(folder, {}).get("state") == "running":
        return {"ok": False, "error": "이미 분리 작업을 진행 중입니다"}

    log_file = ms.LOGS / f"resplit-{folder}.log"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"{folder}-transcript-speakers.md"
    shutil.copy2(spk_out, backup)
    set_job(folder, kind="재분리", state="running", started=ms.now_iso(), stage=1, stages=1,
            stage_label=f"화자 {n}명으로 다시 분리하는 중",
            message=f"화자 {n}명으로 다시 분리하는 중입니다 (수 분). 이 화면을 닫아도 계속됩니다")

    def worker():
        try:
            with log_file.open("w", encoding="utf-8") as fh:
                fh.write(f"# 화자 수 재분리 | {folder} | {n}명 | {ms.now_iso()}\n")
                try:
                    code = run_diarize(fh, audios[0], vtt, spk_out, n)
                except subprocess.TimeoutExpired:
                    code = -1
                    fh.write("\n[server] 제한 시간을 넘겨 끊었습니다\n")
                good = code == 0 and spk_out.exists() and spk_out.stat().st_size > 0
                if not good:
                    shutil.copy2(backup, spk_out)
                    fh.write("\n[server] 실패. 이전 분리본으로 되돌렸습니다\n")
            backup.unlink(missing_ok=True)
            local_hint = reapply_local(folder, spk_out, d / ms.DRAFT_NAME) if good else ""
            if good:
                text = spk_out.read_text(encoding="utf-8", errors="replace")
                rows = [lb for lb in talk_rows(text) if lb != UNKNOWN_LABEL]
                left = unnamed_labels(text)
                hint = (f" 이름이 없는 화자 {len(left)}명({', '.join(left)})은 구간을 듣고 등록하세요."
                        if left else " 모든 화자에 이름이 붙었습니다.")
                draft_hint = (" 분리본이 초안보다 최신이 됐습니다. '다시 생성'을 권합니다."
                              if (d / ms.DRAFT_NAME).exists() else "")
                set_job(folder, state="done",
                        message=f"화자 {n}명으로 다시 분리했습니다. 결과 {len(rows)}명.{hint}"
                                f"{local_hint}{draft_hint}")
            else:
                set_job(folder, state="failed",
                        message=f"다시 분리 실패 (종료 코드 {code}). 이전 분리본으로 되돌렸습니다."
                                f" 로그: {log_file.name}")
        except Exception as exc:                   # noqa: BLE001
            if backup.exists():
                shutil.copy2(backup, spk_out)
                backup.unlink(missing_ok=True)
            set_job(folder, state="failed", message=f"{type(exc).__name__}: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "message": f"화자 {n}명으로 다시 분리를 시작했습니다 (수 분)"}


# ---------------------------------------------------------------- 등록부 관리 (스펙 3-4 · 5-4)

def registry_backup() -> Path:
    """고치기 전 사본. 등록부가 깨지면 이후 모든 회의의 화자 인식이 죽는데 Git 미추적이라 복구처가 없다."""
    ms.VOICES_BACKUP.mkdir(parents=True, exist_ok=True)
    # 초 단위 이름이면 같은 초에 두 번 고칠 때 앞 사본이 덮인다 (실측: 이름 변경 직후 삭제).
    dst = ms.VOICES_BACKUP / f"{datetime.now(ms.TZ).strftime('%Y%m%d-%H%M%S-%f')}.json"
    if ms.VOICES.exists():
        shutil.copy2(ms.VOICES, dst)
    return dst


def last_seen(names: set[str]) -> dict[str, str]:
    """이름마다 마지막으로 그 이름이 붙은 회의 폴더. 분리본 발화량 표만 훑는다."""
    out: dict[str, str] = {}
    if not names or not ms.MEETINGS.is_dir():
        return out
    for d in sorted(ms.MEETINGS.iterdir()):
        spk = d / "transcript-speakers.md"
        if not d.is_dir() or not spk.exists():
            continue
        try:
            rows = set(talk_rows(spk.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
        for n in names & rows:
            out[n] = d.name        # 폴더명이 날짜로 시작하므로 정렬 순서가 곧 시간 순서다
    return out


def registry_state() -> dict:
    reg = ms.read_json(ms.VOICES, {}) or {}
    seen = last_seen(set(reg))
    items = []
    for name in sorted(reg):
        v = reg[name] if isinstance(reg[name], dict) else {}
        items.append({
            "name": name,
            "seconds": v.get("seconds"),
            "source": v.get("source") or "",
            "last_seen": seen.get(name, ""),
            "dim": len(v.get("embedding") or []),
        })
    busy = busy_folders()
    return {"ok": True, "items": items,
            "path": str(ms.VOICES.relative_to(ms.REPO) if ms.VOICES.is_relative_to(ms.REPO) else ms.VOICES),
            "backup_dir": str(ms.VOICES_BACKUP.relative_to(ms.REPO)
                              if ms.VOICES_BACKUP.is_relative_to(ms.REPO) else ms.VOICES_BACKUP),
            "busy": busy,
            "busy_reason": (f"화자 분리가 도는 중입니다 ({', '.join(busy)}). diarize.py 도 같은 파일을 쓰므로"
                            " 지금 고치면 나중에 쓴 쪽이 상대 변경을 덮습니다" if busy else "")}


def registry_edit(action: str, name: str, new_name: str = "", confirm: str = "") -> dict:
    """등록부 이름 변경 · 삭제. 쓰기 전에 사본을 남기고, 분리 실행 중에는 막는다."""
    busy = busy_folders()
    if busy:
        return {"ok": False, "error": f"화자 분리가 도는 중이라 등록부를 고칠 수 없습니다 ({', '.join(busy)})."
                                      " 끝나면 다시 열립니다"}
    with ms.path_lock(ms.VOICES):
        reg = ms.read_json(ms.VOICES, {}) or {}
        if name not in reg:
            return {"ok": False, "error": f"등록부에 없는 이름입니다: {name}"}
        if action == "delete":
            if confirm != name:
                return {"ok": False, "error": "지우려면 이름을 그대로 입력하세요"}
            backup = registry_backup()
            reg.pop(name)
            ms.write_json(ms.VOICES, reg)
            return {"ok": True, "message": f"{name} 을(를) 지웠습니다. 사본: {backup.name}."
                                           " 이미 이름이 붙은 과거 분리본은 그대로 남고,"
                                           " 지운 것은 다음 분리부터 반영됩니다"}
        if action == "rename":
            new = (new_name or "").strip()
            if not new:
                return {"ok": False, "error": "새 이름이 비어 있습니다"}
            if new == name:
                return {"ok": False, "error": "이름이 그대로입니다"}
            if new in reg:
                return {"ok": False, "error": f"이미 있는 이름입니다: {new}. 병합은 하지 않습니다"}
            backup = registry_backup()
            reg[new] = reg.pop(name)
            ms.write_json(ms.VOICES, reg)
            return {"ok": True, "message": f"{name} → {new} 로 바꿨습니다. 사본: {backup.name}."
                                           " 과거 분리본의 이름은 그대로 남습니다"}
    return {"ok": False, "error": "알 수 없는 동작입니다"}


# ---------------------------------------------------------------- 설치 마법사 (스펙 5-5)

def setup_finish() -> dict:
    """7단계 완료 화면에 필요한 것. 다음 녹음 예정은 캐시에 있는 오늘 일정에서 고른다."""
    today = today_schedule()
    nxt = next_event([e for e in (today.get("events") or []) if e.get("record")])
    return {
        "autorecord": autorecord_on(),
        "plist": RECORDER_PLIST.exists(),
        "next": {"when": nxt["when"], "title": nxt["title"]} if nxt else None,
        "schedule_error": today.get("error"),
        "login_item": sw.login_item_on(),
        "warnings": ["노트북 뚜껑을 닫으면 맥이 잠들어 녹음이 끊깁니다. 회의 중에는 열어 두세요",
                     "마이크만 녹음됩니다. 온라인 회의 상대방 목소리는 담기지 않습니다",
                     "녹음 전에 참석자 동의를 받으세요"],
    }


def setup_post(action: str, data: dict) -> dict:
    """마법사의 POST 동작. 단계 결과는 전부 `.state/setup.json` 에 남는다 (기준 43)."""
    if action == "step":
        try:
            step = int(data.get("step", 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "단계 번호가 아닙니다"}
        if not 1 <= step <= 7:
            return {"ok": False, "error": "단계는 1~7 입니다"}
        status = str(data.get("status", "pass"))
        if status not in ("pass", "fail", "skip"):
            return {"ok": False, "error": "상태는 pass · fail · skip 입니다"}
        return {"ok": True, "state": sw.mark(step, status, str(data.get("detail", "")))}
    if action == "reset":
        return {"ok": True, "state": sw.reset()}
    if action == "install":
        return {"ok": True, "job": sw.install_tools(), "name": "install"}
    if action == "models":
        return {"ok": True, "job": sw.download_models(), "name": "models"}
    if action == "mic":
        got = sw.mic_step()
        sw.mark(5, "pass" if got["ok"] else "fail",
                (got.get("launchd") or got["foreground"]).get("reason", ""))
        return {"ok": got["ok"], **got}
    if action == "mic-cleanup":
        return {"ok": True, **sw.cleanup_mictest()}
    if action == "ics":
        got = sw.save_ics(str(data.get("url", "")))
        if got["ok"]:
            today = today_schedule(force=True)
            evs = decorate_events(today.get("events") or [], set(ms.list_folders()), "",
                                  datetime.now(ms.TZ).strftime("%Y-%m-%d"))
            got["events"] = [{"when": e["when"], "title": e["title"], "room": e["room"],
                              "record": e["record"], "skip_reason": e["skip_reason"]} for e in evs]
            got["count"] = len(evs)
            got["error"] = today.get("error")
            sw.mark(6, "pass", f"오늘 일정 {len(evs)}건")
        else:
            sw.mark(6, "fail", got["reason"])
        return got
    if action == "menubar":
        got = sw.launch_menubar()
        return got
    if action == "login-item":
        # 자동으로 켜지 않는다. 이 경로는 사람이 마법사에서 고를 때만 불린다 (스펙 8절 결정 7)
        return sw.login_item(bool(data.get("on")))
    if action == "finish":
        return {"ok": True, **setup_finish()}
    return {"ok": False, "error": "없는 동작입니다"}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "meeting-console"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):             # 표준 stderr 접속 로그는 끈다
        pass

    def log_request(self, code="-", size="-"):
        """최소 접근 로그: 시각 · 메서드 · 경로 · 상태. **쿼리(토큰)는 남기지 않는다.**

        2026-09-04 마이크 권한 대화상자가 예고 없이 떠서 원인을 못 잡았다(접근 로그가 없었다).
        누가 어떤 API 를 언제 쳤는지만 남긴다. 파일: logs/access.log
        """
        try:
            path = self.path.split("?", 1)[0]
            ms.LOGS.mkdir(parents=True, exist_ok=True)
            with (ms.LOGS / "access.log").open("a", encoding="utf-8") as f:
                f.write(f"{ms.now_iso()} {MY_PORT} {self.command} {path} {code}\n")
        except Exception:
            pass

    # ---- 응답 도우미
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _token_ok(self) -> bool:
        q = parse_query(self.path.partition("?")[2])
        if q.get("t") == TOKEN:
            return True
        if self.headers.get("X-Console-Token") == TOKEN:
            return True
        cookie = self.headers.get("Cookie") or ""
        return any(c.strip() == f"mc_token={TOKEN}" for c in cookie.split(";"))

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    # ---- 라우팅
    def do_GET(self) -> None:                      # noqa: N802
        path, _, qs = self.path.partition("?")
        q = parse_query(qs)
        if not self._token_ok():
            self._json(401, {"error": "토큰이 필요합니다. 콘솔을 띄운 터미널의 주소로 여세요"})
            return

        if path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            self._send(200, body, "text/html; charset=utf-8",
                       {"Set-Cookie": f"mc_token={TOKEN}; Path=/; SameSite=Strict"})
            return
        if path.startswith("/static/"):
            self._serve_file(STATIC, path[len("/static/"):])
            return
        if path.startswith("/clips/"):
            self._serve_clip(path[len("/clips/"):])
            return
        if path == "/setup" or path == "/setup.html":
            body = (STATIC / "setup.html").read_bytes()
            self._send(200, body, "text/html; charset=utf-8",
                       {"Set-Cookie": f"mc_token={TOKEN}; Path=/; SameSite=Strict"})
            return
        if path == "/api/setup/state":
            # 통과한 단계는 .state/setup.json 에 남는다. 브라우저를 닫아도 이어서 한다 (기준 43)
            self._json(200, {"steps": sw.STEPS, "state": sw.read_state(),
                             "models": sw.model_checks(), "plan": sw.install_plan(),
                             "ics_saved": ICS_URL_FILE.exists(), "howto": sw.ICS_HOWTO,
                             "app": sw.host_app(), "deeplink": sw.MIC_DEEPLINK,
                             "login_item": sw.login_item_on(),
                             "autorecord": autorecord_on(),
                             "meetings_dir": str(ms.MEETINGS.relative_to(ms.REPO))})
            return
        if path == "/api/setup/system":
            self._json(200, sw.system_checks())
            return
        if path == "/api/setup/job":
            self._json(200, sw.job_state(q.get("name") or ""))
            return
        if path == "/api/state":
            self._json(200, full_state())
            return
        if path == "/api/today":
            self._json(200, today_schedule(force=q.get("force") == "1"))
            return
        if path == "/api/week":
            # 캐시에 있는 것만 담아 바로 돌려준다. 없는 날은 화면이 /api/schedule 로 하나씩 받는다
            start = q.get("start") or datetime.now(ms.TZ).strftime("%Y-%m-%d")
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
                self._json(400, {"error": "날짜 형식은 YYYY-MM-DD 입니다"})
                return
            self._json(200, week_payload(start, force=q.get("force") == "1"))
            return
        if path == "/api/week-fetch":
            # 그 주에서 캐시가 없거나 낡은 날을 ICS 1회 다운로드로 한꺼번에 채운다.
            #  하루씩 받던 방식(7회 다운로드)이 13~16초라 기준 41 을 못 넘겼다 (2026-09-04 결정).
            start = q.get("start") or datetime.now(ms.TZ).strftime("%Y-%m-%d")
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
                self._json(400, {"error": "날짜 형식은 YYYY-MM-DD 입니다"})
                return
            force = q.get("force") == "1"
            fetch_week(week_days(start), force=force)
            self._json(200, week_payload(start))
            return
        if path == "/api/schedule":
            # 하루치 ICS 조회. 약 2초 걸린다 (한 번에 한 날만 부르게 화면에서 줄 세운다)
            day = q.get("day") or ""
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
                self._json(400, {"error": "날짜 형식은 YYYY-MM-DD 입니다"})
                return
            data = day_schedule(day, force=q.get("force") == "1")
            rec = ms.current_recording()
            self._json(200, {
                "day": day, "error": data.get("error"), "stale": data.get("stale", False),
                "fetched_at": data.get("fetched_at", ""),
                "events": decorate_events(data.get("events") or [], set(ms.list_folders()),
                                          rec["folder"] if rec else "", day)})
            return
        if path == "/api/event":
            day = q.get("day") or ""
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
                self._json(400, {"error": "날짜 형식은 YYYY-MM-DD 입니다"})
                return
            self._json(200, event_detail(day, q.get("folder", ""), q.get("title", "")))
            return
        if path == "/api/meeting":
            folder = safe_folder(q.get("folder", ""))
            self._json(200, meeting_detail(folder)) if folder else self._json(404, {"error": "없는 폴더"})
            return
        if path == "/api/transcript":
            folder = safe_folder(q.get("folder", ""))
            self._json(200, {"hits": search_transcript(folder, q.get("q", ""))} if folder else {"hits": []})
            return
        if path == "/api/speakers":
            folder = safe_folder(q.get("folder", ""))
            if not folder:
                self._json(404, {"ok": False, "error": "없는 폴더"})
                return
            res = speaker_segments(folder)
            res["enroll"] = _enroll.get(folder, {"state": "idle", "message": ""})
            self._json(200, res)
            return
        if path == "/api/registry":
            self._json(200, registry_state())
            return
        if path == "/api/speakers/candidates":
            cands = [{"folder": m["folder"], "title": m["title"], "state": m["label"]}
                     for m in ms.list_meetings() if m["has_speakers"]]
            self._json(200, {"candidates": cands})
            return
        self._json(404, {"error": "없는 경로"})

    def do_HEAD(self) -> None:                     # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:                     # noqa: N802
        path, _, qs = self.path.partition("?")
        if not self._token_ok():
            self._json(401, {"error": "토큰이 필요합니다"})
            return
        data = self._body()
        folder = safe_folder(str(data.get("folder", "")))

        if path == "/api/recording/stop":
            self._json(200, stop_recording())
            return
        if path == "/api/autorecord":
            self._json(200, set_autorecord(bool(data.get("on"))))
            return
        if path == "/api/today/refresh":
            self._json(200, today_schedule(force=True))
            return
        if path.startswith("/api/setup/"):
            self._json(200, setup_post(path[len("/api/setup/"):], data))
            return
        if path == "/api/registry":
            self._json(200, registry_edit(str(data.get("action", "")), str(data.get("name", "")),
                                          str(data.get("new_name", "")), str(data.get("confirm", ""))))
            return
        if not folder:
            self._json(400, {"ok": False, "error": "폴더를 찾지 못했습니다"})
            return
        if path == "/api/approve":
            res = approve(folder, data.get("text", ""), data.get("index_row", {}))
            # 입력이 잘못된 것(인덱스 행이 dict 아님)은 400 으로 돌려준다. 나머지는 200 + ok 플래그.
            self._json(res.pop("status", 200), res)
            return
        if path == "/api/exclude":
            self._json(200, exclude(folder, data.get("reason", ""),
                                    bool(data.get("delete")), str(data.get("confirm", ""))))
            return
        if path == "/api/regenerate":
            self._json(200, regenerate(folder))
            return
        if path == "/api/enroll":
            # 등록부 저장은 기본 켬 (스펙 8절 결정 2). 화면에서 끄면 이 회의에만 이름이 붙는다
            self._json(200, enroll(folder, data.get("names", {}),
                                   data.get("save_to_registry", True) is not False))
            return
        if path == "/api/resplit":
            try:
                n = int(data.get("speakers", 0))
            except (TypeError, ValueError):
                n = 0
            self._json(200, resplit(folder, n))
            return
        self._json(404, {"error": "없는 경로"})

    # ---- 정적 파일 · 클립
    def _resolve(self, base: Path, rel: str) -> Path | None:
        target = (base / pct(rel).lstrip("/")).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            return None
        return target if target.is_file() else None

    def _serve_file(self, base: Path, rel: str) -> None:
        target = self._resolve(base, rel)
        if not target:
            self._json(404, {"error": "없는 파일"})
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _serve_clip(self, rel: str) -> None:
        """Range 요청을 받는다. 브라우저 오디오 탐색이 206 을 요구한다 (수용 기준 20)."""
        target = self._resolve(ms.CLIP_DIR, rel)
        if not target:
            self._json(404, {"error": "없는 클립"})
            return
        data = target.read_bytes()
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and (m.group(1) or m.group(2)):
            size = len(data)
            start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2)))
            end = int(m.group(2)) if m.group(1) and m.group(2) else size - 1
            end = min(end, size - 1)
            if start > end:
                self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                return
            chunk = data[start:end + 1]
            self._send(206, chunk, "audio/mp4",
                       {"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes"})
            return
        self._send(200, data, "audio/mp4", {"Accept-Ranges": "bytes"})


# ---------------------------------------------------------------- 기동

def clear_clips() -> None:
    """임시 클립은 서버가 끝날 때 지운다 (스펙 5-3, 수용 기준 4).

    같은 STATE_DIR 을 쓰는 다른 서버가 살아 있으면 건너뛴다. 그 서버가 지금 서빙 중인 클립을
    지우면 사람이 듣던 구간이 404 가 된다.
    """
    peers = ms.live_token_ports(exclude=MY_PORT)
    if peers:
        print(f"▸ 같은 .state 를 쓰는 서버가 떠 있어({peers}) 임시 클립을 건드리지 않습니다", flush=True)
        return
    if ms.CLIP_DIR.exists():
        shutil.rmtree(ms.CLIP_DIR, ignore_errors=True)
    ms.CLIP_DIR.mkdir(parents=True, exist_ok=True)


def refresh_schedule_cli() -> int:
    """`--schedule [YYYY-MM-DD]`: 일정 캐시만 갱신하고 끝낸다 (서버를 띄우지 않는다).

    메뉴바가 이 경로로 캐시를 채운다. 메뉴바 앱은 rumps 로 도는 별도 venv 라
    icalendar 를 못 쓰고, 캐시 형식과 회의실 판정을 두 곳에 두면 반드시 갈린다.
    ICS 를 받는 코드는 여전히 scripts/calendar-watch.py 한 곳뿐이다 (수용 기준 3).
    """
    ms.STATE_DIR.mkdir(parents=True, exist_ok=True)
    args = [a for a in sys.argv[1:] if re.match(r"^\d{4}-\d{2}-\d{2}$", a)]
    day = args[0] if args else datetime.now(ms.TZ).strftime("%Y-%m-%d")
    data = day_schedule(day, force="--force" in sys.argv)
    print(json.dumps({"day": day, "events": len(data.get("events") or []),
                      "error": data.get("error"), "cache": str(ms.schedule_path(day))},
                     ensure_ascii=False))
    return 0 if not data.get("error") else 1


def main() -> int:
    global TOKEN, TOKEN_FILE, MY_PORT
    if "--schedule" in sys.argv:
        return refresh_schedule_cli()
    ms.STATE_DIR.mkdir(parents=True, exist_ok=True)
    ms.LOGS.mkdir(parents=True, exist_ok=True)

    # ⚠️ 포트를 먼저 잡는다. 토큰 파일 이름이 포트로 정해지고, 뒷정리를 건너뛸지도
    #    "내 포트를 뺀 다른 서버가 살아 있는가"로 판단하기 때문이다.
    httpd = None
    for port in range(PORT_START, PORT_START + PORT_TRIES):
        try:
            httpd = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print(f"❌ {PORT_START}~{PORT_START + PORT_TRIES - 1} 포트가 모두 쓰이고 있습니다", file=sys.stderr)
        return 1

    MY_PORT = httpd.server_port
    TOKEN_FILE = ms.token_path(MY_PORT)
    clear_clips()
    resume_jobs()
    TOKEN = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(TOKEN, encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)

    # kill 로 끝날 때도 뒷정리를 하도록 SIGTERM 을 Ctrl+C 와 같게 다룬다.
    #  SIGINT 는 명시적으로 되살린다: 백그라운드(&)로 띄우면 셸이 SIGINT 를 무시로 물려줘
    #  Ctrl+C 가 안 먹고 임시 클립이 남는다.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    signal.signal(signal.SIGINT, signal.default_int_handler)

    # --open-setup 이면 설치 마법사로 연다 (install.sh 마지막 단계가 이렇게 부른다)
    page = "/setup" if "--open-setup" in sys.argv else "/"
    url = f"http://{HOST}:{httpd.server_port}{page}?t={TOKEN}"
    print(f"▸ 회의 콘솔: {url}", flush=True)
    print("▸ 127.0.0.1 에만 붙습니다. 같은 와이파이의 다른 기기에서는 열리지 않습니다.", flush=True)
    print("▸ 끄려면 Ctrl+C", flush=True)
    if "--no-open" not in sys.argv:
        subprocess.run(["open", url], capture_output=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n▸ 종료합니다")
    finally:
        httpd.server_close()
        # 지우는 것은 **내 포트의 토큰뿐**이다. 앞서 뜬 서버의 토큰을 지우면 그 서버가 살아 있는데도
        #  메뉴바가 "서버 없음"으로 읽고 또 띄운다 (실측: 서버 4개 동시 기동).
        clear_clips()
        TOKEN_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
