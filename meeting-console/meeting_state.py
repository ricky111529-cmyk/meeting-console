"""회의 콘솔 공통 로직 - 폴더 상태 파생 · review.json · 락 · 녹음 감지.

watcher.py 와 server.py 가 같이 쓴다. 표준 라이브러리만 쓴다 (uv 로 돌 때 의존성 0).

**상태는 파일 존재에서 파생하고, 사람이 내린 판정만 review.json 에 적는다** (스펙 3-3).
전역 인덱스나 DB 를 만들지 않는다. 폴더와 인덱스가 어긋나면 사람이 손으로 못 고친다.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
import pathlib
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Seoul")

CONSOLE = Path(__file__).resolve().parent


def _find_repo() -> Path:
    """레포 루트를 찾는다. 번들로 배포되면 경로 깊이가 달라질 수 있어 위로 훑는다."""
    env = os.environ.get("MEETING_CONSOLE_REPO")
    if env and (Path(env) / "docs" / "meetings").is_dir():
        return Path(env).resolve()
    for p in [CONSOLE, *CONSOLE.parents]:
        if (p / "docs" / "meetings").is_dir() and (p / "scripts").is_dir():
            return p
    return CONSOLE.parents[2]


REPO = _find_repo()
MEETINGS = REPO / "docs" / "meetings"
SCRIPTS = REPO / "scripts"
INDEX_MD = MEETINGS / "README.md"
# 목소리 등록부와 화자 분리 스크립트는 환경변수로 갈아끼울 수 있다.
#  실제 등록부(이름 → 임베딩)가 오염되면 이후 모든 회의의 자동 인식이 틀어지므로,
#  검증은 임시 등록부·임시 diarize.py 사본을 가리켜 돌린다. 평상시에는 아래 기본값이다.
#  MEETING_CONSOLE_DIARIZE 를 사본으로 두면 diarize.py 가 제 위치(스크립트의 부모의 부모)를
#  레포로 보므로 등록부도 그 사본 쪽을 쓴다. scripts/ 는 고치지 않는다.
VOICES = Path(os.environ.get("MEETING_CONSOLE_VOICES")
              or REPO / ".claude" / "voice-registry" / "voices.json")
DIARIZE = Path(os.environ.get("MEETING_CONSOLE_DIARIZE") or SCRIPTS / "diarize.py")
VOICES_BACKUP = None            # 아래 STATE_DIR 정의 뒤에 채운다

LOGS = CONSOLE / "logs"
# 런타임 상태(토큰 · 임시 클립 · 분리 작업 기록 · 일정 캐시)를 담는 곳도 갈아끼울 수 있다.
#  검증용 서버를 띄울 때 이것을 다른 경로로 두지 않으면 **사람이 쓰고 있는 서버의 토큰을 덮고
#  임시 클립과 분리 작업 기록까지 지운다** (실측: 검증 중 서버 4개가 동시에 떴다).
#  MEETING_CONSOLE_STATE 를 주면 그 아래에 token-{포트} · clips · jobs.json 이 따로 생긴다.
STATE_DIR = Path(os.environ.get("MEETING_CONSOLE_STATE") or CONSOLE / ".state")
VERDICT_DIR = STATE_DIR / "verdict"
CLIP_DIR = STATE_DIR / "clips"
VOICES_BACKUP = STATE_DIR / "voices-backup"     # 등록부를 고치기 전 사본 (스펙 3-4)
EXCLUDE_RULES = CONSOLE / "exclude-rules.json"
PROMPT_FILE = CONSOLE / "prompts" / "draft-note.md"


# ---------------------------------------------------------------- 서버 토큰 (인스턴스별)

# 콘솔 서버가 쓰는 포트 범위. server.py 가 PORT_START 부터 비는 포트를 찾아 잡는다.
#  서버를 찾는 일은 이 범위를 훑는 것으로만 한다 (아래 console_ports 주석).
PORT_START = 7788
PORT_TRIES = 10


LSOF = "/usr/sbin/lsof" if pathlib.Path("/usr/sbin/lsof").exists() else "lsof"   # PATH 에 /usr/sbin 이 없는 환경에서 FileNotFoundError 를 막는다


def token_path(port: int) -> Path:
    """토큰 파일은 포트마다 따로 둔다.

    한 파일(`.state/token`)을 쓰면 뒤에 뜬 서버가 앞 서버의 토큰을 덮고, 종료하면서
    **살아 있는 앞 서버의 토큰까지 지운다.** 그러면 메뉴바가 "토큰이 없으니 서버가 없다"로
    읽고 서버를 또 띄운다 (실측: 서버 4개 동시 기동). 포트는 인스턴스마다 다르므로 짝이 맞는다.
    """
    return STATE_DIR / f"token-{port}"


def port_alive(port: int) -> bool:
    """그 포트를 누군가 듣고 있나.

    **프로세스가 콘솔 서버인지까지 따지지 않는다.** pgrep 패턴은 절대 경로로 띄운 서버만
    잡는데(터미널에서 `uv run server.py` 로 띄우면 명령줄에 경로가 없다), 그것을 죽은 것으로
    보면 살아 있는 서버의 토큰 파일을 지운다 (실측). 쓰이는 포트의 토큰은 남기는 쪽으로 기운다.
    """
    r = subprocess.run([LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def listening_ports() -> set[int]:
    """콘솔 포트 범위에서 지금 누가 듣고 있는 포트. lsof 한 번으로 범위를 통째로 훑는다."""
    span = f"{PORT_START}-{PORT_START + PORT_TRIES - 1}"
    r = subprocess.run([LSOF, "-nP", f"-iTCP:{span}", "-sTCP:LISTEN"],
                       capture_output=True, text=True)
    return {int(m) for m in re.findall(r"127\.0\.0\.1:(\d+) \(LISTEN\)", r.stdout)}


def console_ports(exclude: int | None = None) -> list[int]:
    """지금 떠 있는 콘솔 서버의 포트.

    **명령줄 문자열로 찾지 않는다** (스펙 8절 「qa 1회차 수정 라운드 뒤 결정」).
    `pgrep -f "meeting-console/server.py"` 는 절대 경로로 띄운 서버만 잡아서,
    폴더에 들어가 `uv run server.py` 로 띄운 서버를 못 본다. 그러면 메뉴바가 살아 있는
    서버를 못 찾고 하나 더 띄운다.

    대신 포트 범위를 lsof 로 훑고 **`token-{포트}` 파일이 있는 것만** 콘솔 서버로 본다.
    토큰 파일은 서버가 그 포트를 잡은 뒤에 쓰고 끝날 때 지우므로 짝이 맞는다.
    토큰이 있는데 포트가 안 들리면 비정상 종료로 남은 죽은 토큰이라 여기서 지운다.
    """
    if not STATE_DIR.is_dir():
        return []
    live = listening_ports()
    out = []
    for p in sorted(STATE_DIR.glob("token-*")):
        try:
            port = int(p.name.split("-", 1)[1])
        except ValueError:
            continue
        alive = port in live if PORT_START <= port < PORT_START + PORT_TRIES else port_alive(port)
        if not alive:
            p.unlink(missing_ok=True)      # 죽은 토큰
            continue
        if port != exclude:
            out.append(port)
    return out


def live_token_ports(exclude: int | None = None) -> list[int]:
    """이 STATE_DIR 을 같이 쓰는, 지금 살아 있는 서버의 포트 (console_ports 의 옛 이름)."""
    return console_ports(exclude)


def read_token(port: int) -> str:
    """그 포트의 서버가 쓴 토큰. 포트가 다르면 토큰도 다르다."""
    try:
        return token_path(port).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def find_console() -> tuple[int, str] | None:
    """붙을 수 있는 콘솔 서버의 (포트, 토큰). 없으면 None.

    토큰을 아직 안 쓴 기동 직후 서버는 건너뛴다 (포트만 알고 열면 401 이 뜬다).
    """
    for port in console_ports():
        token = read_token(port)
        if token:
            return port, token
    return None


# ---------------------------------------------------------------- 일정 캐시 (서버·메뉴바 공유)

def schedule_path(day: str) -> Path:
    """그 날짜의 일정 캐시. **서버와 메뉴바가 같은 파일을 읽고 쓴다** (스펙 3-2).

    ICS 조회만 비용이 있는 항목이라(실측 1회 약 2초, 1.5MB) 양쪽이 따로 받으면
    같은 것을 두 번 내려받는다. 캐시에 회의실(LOCATION)도 실어 메뉴바 상태 줄이 쓴다.
    """
    return STATE_DIR / f"schedule-{day}.json"


def read_schedule(day: str) -> dict | None:
    data = read_json(schedule_path(day), None)
    return data if isinstance(data, dict) else None


def write_schedule(day: str, data: dict) -> None:
    write_json(schedule_path(day), data)


def schedule_age(data: dict | None) -> float:
    """캐시가 몇 초 지났나. 값이 없으면 무한대."""
    if not data:
        return float("inf")
    try:
        return max(0.0, time.time() - float(data.get("at") or 0))
    except (TypeError, ValueError):
        return float("inf")


SCHEDULE_KEEP_MAX = 120


def prune_schedules(keep_max: int = SCHEDULE_KEEP_MAX) -> None:
    """일정 캐시 파일 수에 상한을 둔다. 날짜별 파일이라 그냥 두면 하루 하나씩 쌓인다.

    **날짜로 자르지 않고 개수로 자른다.** 예전에는 `today-14` 이전 날짜를 지웠는데,
    캘린더 페이지가 임의의 과거 주를 열 수 있게 되면서 그 규칙이 스펙 3-6
    「과거 주는 한 번 받은 것을 세션 동안 유지한다」와 정면으로 어긋났다.
    한 주를 받아 쓴 직후 같은 요청 안에서 그 7개가 지워져, 그리드가 비고
    볼 때마다 1.5MB ICS 를 다시 받았다.

    버리는 순서는 **마지막으로 쓴 시각(mtime)이 오래된 것부터**다. 방금 받은 주는
    항상 살아남고, 오늘 캐시는 5분마다 다시 쓰이므로 가장 나중에 버려진다
    (메뉴바가 읽는 `schedule-{오늘}.json` 은 별도로 한 번 더 지킨다).
    """
    keep_max = max(1, int(keep_max))
    today = datetime.now(TZ).date().isoformat()
    files = []
    for p in STATE_DIR.glob("schedule-*.json"):
        day = p.name[len("schedule-"):-len(".json")]
        if len(day) != 10 or day == today:
            continue                                    # 오늘 캐시는 메뉴바가 읽는다. 지우지 않는다
        try:
            files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    files.sort()                                        # 오래 안 쓴 것이 앞
    for _, p in files[:max(0, len(files) - keep_max)]:
        p.unlink(missing_ok=True)


LOCK_NAME = ".draft.lock"
DRAFT_NAME = "notes.draft.md"
NOTES_NAME = "notes.md"
REVIEW_NAME = "review.json"

# STT 가 끝났는지 판정하는 기준 (스펙 3-2).
#  transcript.md 가 생겨도 화자 분리가 뒤에 따로 돌아 transcript-speakers.md 는 나중에 나온다.
#  분리본이 나오기 전에 초안을 쓰면 발화자가 없는 노트가 된다.
STT_SETTLE_SEC = 120
# 락이 이 시간을 넘고 그 PID 가 죽어 있으면 stale 로 보고 해제한다
LOCK_STALE_SEC = 30 * 60

AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".aac", ".flac", ".mp4", ".aifc", ".aiff")

# 상태 코드 -> 화면 표기. 검수 큐는 이 순서로 묶어 보여준다 (스펙 5-2).
STATES = [
    ("review", "확인 필요"),
    ("suspect", "회의인지 확인"),
    ("drafting", "노트 작성 중"),
    ("failed", "노트 작성 실패"),
    ("draft-wait", "노트 작성 예정"),
    ("stt-wait", "글로 옮기는 중"),
    ("excluded", "제외"),
    ("approved", "확정"),
]
STATE_LABEL = dict(STATES)


# ---------------------------------------------------------------- 파일 도우미

def now_iso() -> str:
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def path_lock(path: Path) -> threading.Lock:
    """파일 하나당 락 하나. 읽고-고쳐-쓰기를 한 덩어리로 묶는 데 쓴다.

    server.py 가 ThreadingHTTPServer 라 같은 파일을 두 스레드가 동시에 만진다.
    락 없이 두면 review.json 병합에서 나중 쓰기가 먼저 쓰기를 덮어 **사람이 내린 판정이 사라진다.**
    """
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS.setdefault(key, threading.Lock())
    return lock


def write_json(path: Path, data) -> None:
    """같은 폴더에 임시 파일로 쓰고 옮긴다 (반쯤 쓰인 JSON 이 남지 않게).

    임시 파일명에 pid 와 난수를 붙인다. 고정 이름(`{name}.tmp`)이면 두 스레드가 같은 임시 파일을
    쓰고 먼저 끝난 쪽이 rename 해 가서 나중 쪽이 FileNotFoundError 로 터진다 (실측: 동시 요청 10건 중 2건 무응답).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():                      # rename 전에 실패한 경우 남기지 않는다
            tmp.unlink(missing_ok=True)


def read_review(folder: str) -> dict:
    return read_json(MEETINGS / folder / REVIEW_NAME, {}) or {}


def write_review(folder: str, patch: dict) -> dict:
    """review.json 을 병합해서 쓴다. 기존 키는 유지한다.

    읽기-병합-쓰기 전체를 파일 락으로 묶는다. 사람이 내린 판정이 이 파일에만 있다.
    """
    target = MEETINGS / folder / REVIEW_NAME
    with path_lock(target):
        cur = read_json(target, {}) or {}
        cur.update(patch)
        write_json(target, cur)
    return cur


# ---------------------------------------------------------------- 락

def lock_path(folder: str) -> Path:
    return MEETINGS / folder / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_info(folder: str) -> dict | None:
    """살아 있는 락이면 내용을, stale 이면 지우고 None 을 준다."""
    p = lock_path(folder)
    if not p.exists():
        return None
    info = read_json(p, {}) or {}
    started = float(info.get("started_ts") or 0)
    age = time.time() - started if started else 0
    pid = int(info.get("pid") or 0)
    if age > LOCK_STALE_SEC and not _pid_alive(pid):
        p.unlink(missing_ok=True)
        return None
    info["age_sec"] = int(age)
    return info


def acquire_lock(folder: str, note: str = "") -> bool:
    """이미 락이 있으면 False. 중복 실행 방지는 이 파일 하나로 한다.

    O_EXCL 로 만든다. 존재 확인 후 쓰기로 두면 워처 두 개가 같은 순간에 통과해 claude 가 둘 뜬다.
    """
    if lock_info(folder) is not None:
        return False
    p = lock_path(folder)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "started": now_iso(),
                          "started_ts": time.time(), "note": note},
                         ensure_ascii=False, indent=2) + "\n"
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return True


def release_lock(folder: str) -> None:
    lock_path(folder).unlink(missing_ok=True)


# ---------------------------------------------------------------- 프로세스 감지

_PGREP_CACHE: dict[str, tuple[float, list[str]]] = {}
_PGREP_TTL = 2.0   # 제어판이 5초마다 폴링한다. 폴더마다 pgrep 을 새로 부르면 낭비다


def _pgrep(pattern: str) -> list[str]:
    """pgrep -fl 결과를 줄 목록으로. 없으면 빈 목록. 2초 캐시."""
    hit = _PGREP_CACHE.get(pattern)
    if hit and time.time() - hit[0] < _PGREP_TTL:
        return hit[1]
    r = subprocess.run(["pgrep", "-fl", pattern], capture_output=True, text=True)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []
    _PGREP_CACHE[pattern] = (time.time(), lines)
    return lines


def stt_in_progress(folder: str) -> str:
    """이 폴더의 STT·화자 분리가 아직 돌고 있으면 그 이유를, 아니면 빈 문자열.

    화자 분리(diarize.py)는 STT 뒤에 따로 돈다. transcript.md 가 생겼다고 끝난 게 아니다.
    transcribe.sh 도 같이 본다 (분리 시작 직전 틈을 메운다).
    """
    for pat, why in (("diarize.py", "화자 분리 실행 중"), ("transcribe.sh", "STT 실행 중")):
        for line in _pgrep(pat):
            if folder in line:
                return why
    return ""


def _parse_etime(t: str) -> int:
    """ps 의 etime 을 초로. "MM:SS" · "HH:MM:SS" · "DD-HH:MM:SS"."""
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        days = int(d)
    parts = [int(x) for x in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def current_recording() -> dict | None:
    """지금 녹음 중인 것. 없으면 None.

    판정 기준은 scripts/calendar-watch.py 의 current_recording() 과 같다 (스펙 5-2).
    프로세스 존재 여부만 보지 않고 **무엇을 얼마나 더 녹음하는지**를 본다.
    (그 함수는 icalendar 의존성을 끌고 있어 import 하지 못한다. 규칙만 같게 유지한다.)
    """
    r = subprocess.run(["pgrep", "-f", "scripts/bin/recorder"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    for pid in r.stdout.split():
        ps = subprocess.run(["ps", "-p", pid, "-o", "etime=,command="],
                            capture_output=True, text=True)
        line = ps.stdout.strip()
        if not line:
            continue
        etime, _, cmd = line.partition(" ")
        args = cmd.split()
        # ⚠️ record.sh 는 `caffeinate -i .../bin/recorder <파일> <초>` 로 띄운다. 그래서
        #    pgrep -f "scripts/bin/recorder" 가 recorder 와 caffeinate 를 **둘 다** 집는다
        #    (실측: recorder 12041 / caffeinate 12043, 명령줄이 같아 문자열 검사로는 구분되지 않는다).
        #    caffeinate 의 pid 를 녹음 프로세스로 잘못 잡으면 "지금 녹음 중지"가 caffeinate 를 끊고
        #    recorder 가 고아로 남아 미완성 파일에서 STT 가 시작된다. 첫 토큰이 recorder 인 것만 집는다.
        if not args or Path(args[0]).name != "recorder":
            continue
        paths = [a for a in args if a.endswith(".m4a")]
        if not paths:
            continue
        audio = Path(paths[0])
        elapsed = _parse_etime(etime.strip())
        total = int(args[-1]) if args and args[-1].isdigit() else None
        return {
            "pid": int(pid),
            "folder": audio.parent.name,
            "audio": str(audio),
            "rel": str(audio.parent.relative_to(REPO)) if audio.is_relative_to(REPO) else str(audio.parent),
            "elapsed": elapsed,
            "total": total,
            "remaining": max(0, total - elapsed) if total is not None else None,
        }
    return None


# ---------------------------------------------------------------- 폴더 상태

def audio_files(d: Path) -> list[Path]:
    return sorted(p for p in d.glob("audio*") if p.suffix.lower() in AUDIO_EXTS)


_DUR_CACHE = STATE_DIR / "durations.json"


def audio_seconds(path: Path) -> float | None:
    """ffprobe 로 잰 길이(초). 파일 mtime 별로 캐시한다 (40MB 를 매번 열지 않게).

    캐시 키에 mtime 이 들어가므로 파일이 자라면 키가 계속 새로 생긴다. 그래서 쓸 때마다
    같은 경로의 옛 키와 사라진 파일의 키를 정리한다 (실측: 녹음 중 폴링만으로 4개 → 20개).
    녹음이 진행 중인 파일은 아예 재지 않는다 (list_meetings 참조).
    """
    key = f"{path}:{int(path.stat().st_mtime)}"
    cache = read_json(_DUR_CACHE, {}) or {}
    if key in cache:
        return cache[key]
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        val = float(r.stdout.strip())
    except ValueError:
        val = None
    with path_lock(_DUR_CACHE):
        cache = read_json(_DUR_CACHE, {}) or {}
        cache[key] = val
        write_json(_DUR_CACHE, prune_duration_cache(cache))
    return val


def prune_duration_cache(cache: dict) -> dict:
    """없어진 파일의 키와 같은 경로의 옛 mtime 키를 버린다. 경로마다 가장 큰 mtime 만 남긴다."""
    newest: dict[str, int] = {}
    for key in cache:
        p, _, stamp = key.rpartition(":")
        if not p or not stamp.isdigit():
            continue
        if not Path(p).is_file():
            continue
        newest[p] = max(newest.get(p, -1), int(stamp))
    return {f"{p}:{stamp}": cache[f"{p}:{stamp}"] for p, stamp in newest.items()}


def meeting_title(folder: str, d: Path) -> str:
    """폴더 안 재료에서 회의 제목을 찾는다. 없으면 폴더명 뒷부분."""
    att = d / "attendees.md"
    if att.exists():
        head = att.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        if head:
            # "> 캘린더 참석자 (자동 기록) | {제목} | {날짜}"
            parts = head[0].split("|")
            if len(parts) >= 3:
                return parts[1].strip()
    notes = d / NOTES_NAME
    if notes.exists():
        for line in notes.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
            if line.startswith("# "):
                return line[2:].strip()
    return folder.split("_", 1)[-1]


def derive(folder: str) -> dict:
    """폴더 하나의 상태. 파일 존재 + review.json 판정으로 정한다 (스펙 3-3)."""
    d = MEETINGS / folder
    review = read_review(folder)
    lock = lock_info(folder)
    audios = audio_files(d)
    has_transcript = (d / "transcript.md").exists()
    info = {
        "folder": folder,
        "date": folder[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", folder) else "",
        "title": meeting_title(folder, d),
        "rel": f"docs/meetings/{folder}",
        "has_audio": bool(audios),
        "audio": audios[0].name if audios else "",
        "has_transcript": has_transcript,
        "has_speakers": (d / "transcript-speakers.md").exists(),
        "has_attendees": (d / "attendees.md").exists(),
        "has_draft": (d / DRAFT_NAME).exists(),
        "has_notes": (d / NOTES_NAME).exists(),
        "late_start": (d / "late-start.txt").exists(),
        "review": review,
        "lock": lock,
        "reason": review.get("reason", ""),
    }

    status = review.get("status", "")
    if info["has_notes"]:
        state = "approved"
    elif status == "excluded":
        state = "excluded"
    elif lock:
        state = "drafting"
    elif status == "needs-human-check":
        state = "suspect"
    elif info["has_draft"]:
        state = "review"
    elif status == "failed" or (review.get("draft", {}).get("exit") not in (None, 0)):
        state = "failed"
    elif has_transcript:
        state = "draft-wait"
        pending = stt_in_progress(folder)
        if pending:
            state = "stt-wait"
            info["reason"] = pending
        else:
            age = time.time() - (d / "transcript.md").stat().st_mtime
            if age < STT_SETTLE_SEC:
                state = "stt-wait"
                info["reason"] = f"STT 안정화 대기 ({int(STT_SETTLE_SEC - age)}초 남음)"
    elif info["has_audio"]:
        state = "stt-wait"
        info["reason"] = info["reason"] or "원문 없음 (글로 옮기는 중 또는 진행 중)"
    else:
        state = "stt-wait"
        info["reason"] = info["reason"] or "오디오 없음"

    info["state"] = state
    info["label"] = STATE_LABEL[state]
    return info


def list_folders() -> list[str]:
    if not MEETINGS.is_dir():
        return []
    return sorted((p.name for p in MEETINGS.iterdir()
                   if p.is_dir() and not p.name.startswith(".")), reverse=True)


def list_meetings(with_duration: bool = False) -> list[dict]:
    # 녹음 중인 폴더는 길이를 재지 않는다. 파일이 자라는 중이라 값이 매번 달라져 ffprobe 가
    #  폴링마다 돌고 캐시 키만 쌓인다. 녹음 중 표시는 "지금 상태"가 이미 보여준다.
    rec = current_recording() if with_duration else None
    recording_folder = rec["folder"] if rec else ""
    out = []
    for folder in list_folders():
        info = derive(folder)
        info["recording_now"] = folder == recording_folder
        if with_duration and info["has_audio"] and not info["recording_now"]:
            sec = audio_seconds(MEETINGS / folder / info["audio"])
            info["duration_min"] = round(sec / 60) if sec else None
        out.append(info)
    return out


# ---------------------------------------------------------------- 제외 규칙

def load_exclude_rules() -> dict:
    """제목 키워드 · 참석자 이메일 패턴 · 폴더 slug 패턴 (스펙 3-4 둘째 겹).

    2026-09-01 결정: **기본 키워드를 두지 않는다.** 빈 배열로 시작하고 사람이 채운다.
    그래서 자동 방어선은 초안 프롬프트의 원문 유형 판정 하나뿐이다.
    """
    data = read_json(EXCLUDE_RULES, None)
    if not isinstance(data, dict):
        data = {}
    return {
        "title_keywords": data.get("title_keywords", []),
        "attendee_patterns": data.get("attendee_patterns", []),
        "slug_patterns": data.get("slug_patterns", []),
    }


def match_exclude(folder: str) -> str:
    """걸리면 사유 문자열, 아니면 빈 문자열. 판정 재료는 attendees.md 와 폴더명뿐이다."""
    rules = load_exclude_rules()
    d = MEETINGS / folder
    title = meeting_title(folder, d)
    for kw in rules["title_keywords"]:
        if kw and kw in title:
            return f"제목 키워드 '{kw}'에 걸림 (제목: {title})"
    for pat in rules["slug_patterns"]:
        try:
            if pat and re.search(pat, folder):
                return f"폴더 slug 패턴 '{pat}'에 걸림"
        except re.error:
            continue
    att = d / "attendees.md"
    if att.exists():
        text = att.read_text(encoding="utf-8", errors="replace")
        for pat in rules["attendee_patterns"]:
            try:
                if pat and re.search(pat, text):
                    return f"참석자 패턴 '{pat}'에 걸림"
            except re.error:
                continue
    return ""


# ---------------------------------------------------------------- 알림

def notify(title: str, message: str) -> None:
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)
