# /// script
# requires-python = ">=3.11"
# dependencies = ["rumps"]
# ///
"""회의 파이프라인 메뉴바 앱 (meeting-console v2 1단계).

맥 메뉴바에 상주하며 녹음 상태·다음 녹음 대상·확인 필요 건수를 보여주고,
콘솔 열기 · 녹음 중지 · 자동 녹음 토글을 한 자리에서 한다.

  실행:        uv run outputs/vibe-coding/meeting-console/menubar/meeting-menubar.py
  상태만 확인: uv run outputs/vibe-coding/meeting-console/menubar/meeting-menubar.py --status

**상태는 콘솔 서버에서 받지 않는다** (스펙 3-2). 서버는 필요할 때만 뜨는 것으로 정해져 있어
`/api/state` 폴링으로 만들면 서버가 꺼진 대부분의 시간에 제목이 빈다. 대신 서버와 워처가
쓰는 것과 같은 모듈(`meeting_state.py` · `server.py`)을 import 해서 판정을 한 곳으로 모은다.
녹음 판정은 `meeting_state.current_recording()` 이고, 이것은 `scripts/calendar-watch.py` 의
같은 이름 함수와 같은 기준이다 (프로세스 존재만 보지 않고 어느 폴더를 얼마나 더 녹음하는지 본다).

`automation-menubar/menubar.py` 와는 별개 앱이다 (스펙 3-1). 코드를 공유하지 않고
검증된 패턴만 같은 방식으로 쓴다: launchctl bootstrap/bootout 토글, rumps 타이머, 위치 기억.

로그인 항목에 스스로 등록하지 않는다 (스펙 8절 결정 7). 상주 등록은 설치 마법사가 묻는다.
"""

from __future__ import annotations

import json
import math
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MENUBAR_DIR = Path(__file__).resolve().parent
CONSOLE = MENUBAR_DIR.parent

# PEP 723 의존성(rumps)과 콘솔 모듈은 경로가 갈린다. rumps 는 uv 가 만든 venv 에 있고
#  meeting_state · server 는 이 레포 파일이다. 둘 다 표준 import 로 잡히게 콘솔 폴더를
#  sys.path 앞에 넣는다. 콘솔 모듈이 표준 라이브러리만 쓰므로 venv 를 오염시키지 않는다.
sys.path.insert(0, str(CONSOLE))

import meeting_state as ms          # noqa: E402
import server as console            # noqa: E402  (판정·실행 로직 재사용. 서버를 띄우지는 않는다)

SERVER_PY = CONSOLE / "server.py"
SERVER_LOG = ms.LOGS / "menubar-server.log"
RECORDER_LABEL = "com.meeting-console.meeting-recorder"

TICK_SEC = 10                       # 녹음 중 갱신 주기 (스펙 3-2)
SLOW_SEC = 60                       # 녹음 중이 아닐 때 갱신 주기
SCHEDULE_TTL = 300                  # 일정 캐시 5분 (스펙 3-2)
TITLE_MAX = 12                      # 제목 자르는 길이 (스펙 3-2)
SERVER_WAIT_SEC = 25                # 서버 기동을 기다리는 상한

IDLE_ICON = "🎙"
REC_ICON = "🔴"


# ---------------------------------------------------------------- 일정 캐시

def schedule_path(day: str | None = None) -> Path:
    return ms.schedule_path(day or datetime.now(ms.TZ).strftime("%Y-%m-%d"))


def load_schedule(force: bool = False) -> dict:
    """오늘 일정. **콘솔 서버와 같은 캐시 파일을 쓴다** (스펙 3-2 · 3단계에서 완성).

    캐시가 5분보다 낡았을 때만 갱신한다. ICS 조회만 비용이 있는 항목이고(1회 약 2초,
    1.5MB) 메뉴바가 60초마다 조회하면 같은 것을 하루 1,440번 내려받는다.

    갱신은 `server.py --schedule` 서브프로세스로 한다. 캘린더 페이지와 같은 경로라
    캐시 형식과 회의실(LOCATION) 판정이 한 곳에서 나온다. **이 파일에는 ICS 를 직접
    받는 코드가 없다** (수용 기준 3). 받는 곳은 scripts/calendar-watch.py 하나뿐이다.
    """
    today = datetime.now(ms.TZ).strftime("%Y-%m-%d")
    cached = ms.read_schedule(today)
    if not force and ms.schedule_age(cached) < SCHEDULE_TTL:
        return cached
    try:
        subprocess.run([console.UV, "run", "--quiet", str(SERVER_PY), "--schedule"],
                       capture_output=True, text=True, timeout=90, cwd=str(ms.REPO))
    except Exception:                               # noqa: BLE001
        pass                                        # 실패 사유는 아래에서 캐시 파일로 읽는다
    fresh = ms.read_schedule(today)
    if fresh:
        return fresh
    return cached or {"at": time.time(), "events": [],
                      "error": "일정 캐시를 갱신하지 못했습니다", "fetched_at": ""}


# ---------------------------------------------------------------- 상태 모으기

def waiting_count() -> int:
    """확인 필요 건수. 콘솔 /api/state 의 waiting 과 같은 정의다.

    **확인 필요만 센다** (스펙 8절 3단계 결정, 2026-09-04). 회의인지 확인은 「처리 상태」
    표로 내려갔으므로 배지에 섞으면 메뉴 건수와 화면 행 수가 어긋난다.
    """
    return sum(1 for m in ms.list_meetings() if m["state"] == "review")


def cut(text: str, n: int = TITLE_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def snapshot(force_schedule: bool = False) -> dict:
    """제목과 메뉴에 필요한 것을 한 번에 모은다."""
    rec = ms.current_recording()
    if rec:
        rec["title"] = ms.meeting_title(rec["folder"], ms.MEETINGS / rec["folder"])
    sched = load_schedule(force=force_schedule)
    # 다음 녹음 대상만 본다. 건너뜀 표시가 붙은 일정은 녹음되지 않으므로 제목에 쓰면 오해를 준다.
    targets = [e for e in sched["events"] if e.get("record")]
    return {
        "recording": rec,
        "next": console.next_event(targets),
        "schedule_error": sched.get("error"),
        "waiting": waiting_count(),
        "autorecord": console.autorecord_on(),
    }


def title_for(snap: dict) -> str:
    """메뉴바 제목 (스펙 3-2 순서)."""
    rec = snap["recording"]
    if rec:
        left = rec.get("remaining")
        if left is None:
            return f"{REC_ICON} 녹음 중"
        return f"{REC_ICON} 남은 {max(1, math.ceil(left / 60))}분"
    nxt = snap["next"]
    if nxt:
        start = (nxt.get("when") or "").split("~")[0]
        return f"{start} {cut(nxt.get('title'))}".strip()
    n = snap["waiting"]
    return f"{IDLE_ICON} {n}" if n else IDLE_ICON


def status_line(snap: dict) -> str:
    """메뉴 첫 줄 (클릭 불가). 지금 무슨 상태인지를 한 문장으로."""
    rec = snap["recording"]
    if rec:
        left = rec.get("remaining")
        tail = f"남은 {max(1, math.ceil(left / 60))}분" if left is not None else "남은 시간 알 수 없음"
        return f"녹음 중: {rec.get('title') or rec['folder']} · {tail}"
    if snap["schedule_error"]:
        return f"일정 조회 실패: {cut(snap['schedule_error'], 40)}"
    nxt = snap["next"]
    if nxt:
        # 회의실을 함께 적는다 (스펙 5-1). 1단계에서는 조회 경로에 LOCATION 이 없어 미뤘고,
        #  3단계에서 캘린더와 같은 캐시를 쓰면서 채워졌다.
        room = (nxt.get("room") or "").strip()
        return f"다음 녹음: {nxt.get('when')} {nxt.get('title')}" + (f" · {cut(room, 24)}" if room else "")
    return "오늘 남은 녹음 대상 없음"


# ---------------------------------------------------------------- 콘솔 열기

def running_server() -> int | None:
    """떠 있는 콘솔 서버의 포트. 없으면 None.

    **명령줄 문자열로 찾지 않는다** (스펙 8절 「qa 1회차 수정 라운드 뒤 결정」).
    `pgrep -f "meeting-console/server.py"` 는 폴더에 들어가 `uv run server.py` 로 띄운
    서버를 못 잡아서, 살아 있는 서버를 못 보고 하나 더 띄웠다. 대신 콘솔 포트 범위를
    lsof 로 훑고 `token-{포트}` 가 있는 것을 콘솔 서버로 본다 (ms.find_console).

    옛 코드(`.state/token` 단일 파일)로 도는 서버를 위한 폴백은 두지 않는다.
    그 폴백이 짝 어긋남을 되살린다. 실서버를 한 번 재시작하면 해소된다.
    """
    found = ms.find_console()
    return found[0] if found else None


def read_token(port: int) -> str:
    """그 포트의 서버가 쓴 토큰. 포트가 다르면 토큰도 다르다."""
    return ms.read_token(port)


def start_server() -> tuple[bool, str]:
    """서버를 띄우고 (성공여부, 메시지). 기동 로그는 파일로 남긴다."""
    ms.LOGS.mkdir(parents=True, exist_ok=True)
    before = set(ms.console_ports())         # 지금 살아 있는 서버 (죽은 토큰 파일은 여기서 치워진다)
    with SERVER_LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n=== 메뉴바에서 기동 {ms.now_iso()} ===\n")
        log.flush()
        subprocess.Popen(
            [console.UV, "run", "--quiet", str(SERVER_PY), "--no-open"],
            stdout=log, stderr=log, cwd=str(ms.REPO), start_new_session=True)
    deadline = time.time() + SERVER_WAIT_SEC
    while time.time() < deadline:
        time.sleep(0.5)
        port = running_server()
        if port and port not in before:
            return True, ""
    return False, f"서버가 {SERVER_WAIT_SEC}초 안에 뜨지 않았습니다. 로그: {SERVER_LOG}"


def open_console(fragment: str = "") -> tuple[bool, str]:
    """콘솔을 브라우저로 연다. 서버가 없으면 띄운 뒤 연다 (스펙 5-1)."""
    found = ms.find_console()
    if not found:
        ok, err = start_server()
        if not ok:
            return False, err
        found = ms.find_console()
    if not found:
        return False, f"서버 포트를 찾지 못했습니다. 로그: {SERVER_LOG}"
    port, token = found
    url = f"http://127.0.0.1:{port}/?t={token}{fragment}"
    subprocess.run(["open", url], capture_output=True)
    return True, url


# ---------------------------------------------------------------- 동작

def stop_recording_now() -> dict:
    """콘솔 서버와 같은 함수를 쓴다. kill -INT 이고 SIGKILL 을 쓰지 않는다.

    강제 종료하면 색인(moov)이 안 쓰여 오디오 데이터가 남아도 파일이 열리지 않는다.
    """
    return console.stop_recording()


def set_autorecord(on: bool) -> dict:
    return console.set_autorecord(on)


# ---------------------------------------------------------------- CLI (GUI 없이 점검)

def print_status() -> int:
    snap = snapshot()
    port = running_server()
    print(f"제목: {title_for(snap)}")
    print(f"상태 줄: {status_line(snap)}")
    print(f"확인 필요: {snap['waiting']}건")
    print(f"자동 녹음: {'켬' if snap['autorecord'] else '끔'}")
    print(f"콘솔 서버: {'포트 %d' % port if port else '꺼짐'}")
    print(f"일정 캐시: {schedule_path()}")
    print(json.dumps(snap, ensure_ascii=False, default=str, indent=2))
    return 0


# ---------------------------------------------------------------- 메뉴바 앱

def main() -> int:
    if "--status" in sys.argv:
        return print_status()

    import rumps

    class MeetingMenuBar(rumps.App):
        def __init__(self):
            super().__init__(IDLE_ICON, quit_button=None)
            self._pinned = False
            self._last_slow = 0.0
            self._snap = {"recording": None, "next": None, "schedule_error": None,
                          "waiting": 0, "autorecord": False}

            self.item_status = rumps.MenuItem("상태 확인 중…")          # 클릭 불가
            self.item_open = rumps.MenuItem("콘솔 열기", callback=self.on_open)
            self.item_stop = rumps.MenuItem("지금 녹음 중지", callback=None)
            self.item_auto = rumps.MenuItem("자동 녹음", callback=self.on_auto)
            self.item_queue = rumps.MenuItem("확인 필요 없음", callback=None)
            self.menu = [
                self.item_status,
                None,
                self.item_open,
                self.item_stop,
                self.item_auto,
                self.item_queue,
                None,
                rumps.MenuItem("새로고침", callback=lambda _: self.refresh(force=True)),
                rumps.MenuItem("종료", callback=lambda _: rumps.quit_application()),
            ]
            self.refresh(force=True)
            # 타이머는 10초 하나로 둔다. 무거운 것(폴더 훑기·일정 조회)만 60초로 건너뛴다.
            #  rumps.Timer 는 도는 중에 주기를 바꾸면 다음 발동이 밀리므로 주기를 고정한다.
            rumps.Timer(lambda _: self.refresh(), TICK_SEC).start()

        def pin_position(self):
            """⌘-드래그로 옮긴 위치를 macOS 가 기억하게 한다. 없으면 재실행마다 오른쪽 끝으로 밀린다."""
            if self._pinned:
                return
            try:
                self._nsapp.nsstatusitem.setAutosaveName_("meeting-console-menubar")
                self._pinned = True
            except Exception:                       # noqa: BLE001
                pass                                # 위치 기억이 안 돼도 앱은 정상 동작

        def refresh(self, force: bool = False):
            self.pin_position()
            slow_due = force or (time.time() - self._last_slow) >= SLOW_SEC
            if slow_due:
                self._snap = snapshot(force_schedule=force)
                self._last_slow = time.time()
            else:
                # 빠른 주기에서는 녹음 상태만 다시 본다 (pgrep 하나. 남은 시간이 분 단위로 틀리지 않게)
                rec = ms.current_recording()
                if rec:
                    rec["title"] = ms.meeting_title(rec["folder"], ms.MEETINGS / rec["folder"])
                was = bool(self._snap.get("recording"))
                self._snap["recording"] = rec
                if was != bool(rec):
                    # 녹음이 시작·종료된 순간은 나머지 상태도 함께 바뀐다
                    self._snap = snapshot()
                    self._last_slow = time.time()

            snap = self._snap
            self.title = title_for(snap)
            self.item_status.title = status_line(snap)

            rec = snap["recording"]
            self.item_stop.set_callback(self.on_stop if rec else None)
            self.item_stop.title = "지금 녹음 중지" if rec else "지금 녹음 중지 (녹음 중이 아님)"

            self.item_auto.title = f"자동 녹음 {'켬' if snap['autorecord'] else '끔'}"
            self.item_auto.state = 1 if snap["autorecord"] else 0

            n = snap["waiting"]
            self.item_queue.title = f"확인 필요 {n}건" if n else "확인 필요 없음"
            self.item_queue.set_callback(self.on_queue if n else None)

        # ---- 메뉴 동작

        def on_open(self, _):
            ok, msg = open_console()
            if not ok:
                rumps.notification("회의 콘솔", "콘솔을 열지 못했습니다", msg)

        def on_queue(self, _):
            # 목적지는 캘린더 첫 화면의 「확인 필요」 영역이다 (기준 28 재정의, 스펙 8절).
            #  1단계에서는 제어판 #queue 였는데, 3단계에서 첫 화면이 캘린더로 바뀌면서
            #  #queue 로 열면 사람이 방금 본 화면을 두고 두 번째 탭으로 튕긴다.
            #  확인 필요 목록은 캘린더 위에도 있으므로 첫 화면에서 바로 집어 들 수 있다.
            ok, msg = open_console("#review")
            if not ok:
                rumps.notification("회의 콘솔", "콘솔을 열지 못했습니다", msg)

        def on_stop(self, _):
            rec = self._snap.get("recording") or {}
            name = rec.get("title") or rec.get("folder") or "지금 녹음"
            if not rumps.alert(title="녹음을 중지할까요?",
                               message=f"{name}\n중지하면 이 시점까지만 녹음됩니다.",
                               ok="중지", cancel="취소"):
                return
            res = stop_recording_now()
            rumps.notification("회의 콘솔", "", res.get("message") or res.get("error") or "")
            self.refresh(force=True)

        def on_auto(self, _):
            res = set_autorecord(not self._snap["autorecord"])
            rumps.notification("회의 콘솔", "",
                               res.get("message") if res.get("ok")
                               else f"바꾸지 못했습니다: {res.get('error')}")
            self.refresh(force=True)

    signal.signal(signal.SIGINT, signal.default_int_handler)
    MeetingMenuBar().run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
