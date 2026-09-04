"""설치 마법사 7단계 (스펙 3-7 · 5-5, 수용 기준 43~49).

server.py 가 이 모듈을 import 해서 `/setup` 화면과 `/api/setup/*` 를 붙인다. 마법사만
따로 둔 이유는 설치가 끝나면 다시 열 일이 없는 코드이기 때문이다.

진행 상태는 `.state/setup.json` 에 둔다. 브라우저를 닫아도 통과한 단계가 남는다 (기준 43).

**바깥으로 나가는 통신은 없다.** 모델 내려받기와 도구 설치는 `brew` · `curl` · `uv` 를
서브프로세스로 부르는 것이고, ICS 검증은 server 가 넘겨준 calendar-watch 함수로만 한다.
ICS 주소는 화면·로그·응답 어디에도 싣지 않는다 (기준 45·42).
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import meeting_state as ms

SETUP_FILE = ms.STATE_DIR / "setup.json"
MICTEST_LABEL = "com.meeting-console.meeting-console-mictest"
MICTEST_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{MICTEST_LABEL}.plist"
MIC_DEEPLINK = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
DIAR_DIR = ms.SCRIPTS / "models" / "diarization"
SEG_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
           "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
EMB_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
           "speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")

STEPS = [
    (1, "시작"), (2, "시스템 점검"), (3, "도구 설치"), (4, "모델 내려받기"),
    (5, "마이크 권한"), (6, "캘린더 연결"), (7, "완료"),
]

# server.py 가 채워 넣는다 (순환 import 를 피한다)
_cw_provider = None
_scrub = lambda exc: f"{type(exc).__name__}: {exc}"   # noqa: E731
_ics_file: Path | None = None
_lock = threading.Lock()
_jobs: dict[str, dict] = {}                           # 단계 이름 -> 진행 중 작업


def bind(cw_provider, scrub, ics_file: Path) -> None:
    """server.py 의 calendar-watch 로더 · 오류 문구 세척기 · ICS 파일 경로를 받는다."""
    global _cw_provider, _scrub, _ics_file
    _cw_provider, _scrub, _ics_file = cw_provider, scrub, ics_file


# ---------------------------------------------------------------- 진행 상태

def read_state() -> dict:
    try:
        return json.loads(SETUP_FILE.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001
        return {"steps": {}}


def mark(step: int, status: str, detail: str = "") -> dict:
    """단계 결과를 기록한다. status 는 pass · fail · skip 셋이다."""
    with _lock:
        data = read_state()
        data.setdefault("steps", {})[str(step)] = {
            "status": status, "detail": detail,
            "at": datetime.now(ms.TZ).strftime("%Y-%m-%d %H:%M:%S")}
        SETUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETUP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data


def reset() -> dict:
    SETUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETUP_FILE.write_text(json.dumps({"steps": {}}, ensure_ascii=False), encoding="utf-8")
    return read_state()


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


# ---------------------------------------------------------------- 2단계 시스템 점검

def _disk_free_gb() -> float:
    return shutil.disk_usage(str(Path.home())).free / 1e9


def system_checks() -> dict:
    """macOS · 칩 · 디스크 · 도구를 한 줄씩 본다. 미통과 항목에는 해결 명령을 그대로 붙인다."""
    mac = run(["sw_vers", "-productVersion"]).stdout.strip() or "?"
    arch = run(["uname", "-m"]).stdout.strip()
    free = _disk_free_gb()
    rows = [
        {"name": "macOS 버전", "ok": mac.split(".")[0].isdigit() and int(mac.split(".")[0]) >= 13,
         "got": mac, "fix": "macOS 13 이상이 필요합니다"},
        {"name": "Apple Silicon", "ok": arch == "arm64", "got": arch,
         "fix": "Intel 맥은 지원하지 않습니다 (whisper·화자 분리가 MLX 를 씁니다)"},
        {"name": "디스크 여유", "ok": free >= 3.0, "got": f"{free:.1f}GB",
         "fix": "3GB 이상 비우고 다시 확인하세요"},
    ]
    for tool, fix in [("brew", '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'),
                      ("ffmpeg", "brew install ffmpeg"),
                      ("uv", "brew install uv"),
                      ("swiftc", "xcode-select --install"),
                      ("claude", "npm install -g @anthropic-ai/claude-code")]:
        path = shutil.which(tool)
        rows.append({"name": tool, "ok": bool(path), "got": path or "없음", "fix": fix})
    blocked = arch != "arm64"
    return {"rows": rows, "blocked": blocked,
            "blocked_reason": "Apple Silicon 이 아니라 여기서 멈춥니다" if blocked else "",
            "ok": all(r["ok"] for r in rows)}


# ---------------------------------------------------------------- 작업 실행 (3·4단계)

def job_state(name: str) -> dict:
    with _lock:
        j = _jobs.get(name)
        return dict(j) if j else {"running": False, "log": [], "ok": None}


def start_job(name: str, steps: list[tuple[str, list[str]]], timeout: int = 1800) -> dict:
    """이름 붙은 명령 여러 개를 순서대로 돌리고 로그를 줄 단위로 모은다 (화면이 폴링한다)."""
    with _lock:
        if _jobs.get(name, {}).get("running"):
            return dict(_jobs[name])
        _jobs[name] = {"running": True, "log": [], "ok": None, "at": time.time()}

    def append(line: str) -> None:
        with _lock:
            _jobs[name]["log"].append(line)
            _jobs[name]["log"] = _jobs[name]["log"][-400:]

    def worker() -> None:
        ok = True
        for label, cmd in steps:
            append(f"$ {label}")
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1)
                for line in proc.stdout:                # type: ignore[union-attr]
                    append(line.rstrip())
                proc.wait(timeout=timeout)
                if proc.returncode != 0:
                    append(f"실패 (종료 코드 {proc.returncode})")
                    ok = False
                    break
            except Exception as exc:                    # noqa: BLE001
                append(f"실패: {type(exc).__name__}: {exc}")
                ok = False
                break
        with _lock:
            _jobs[name]["running"] = False
            _jobs[name]["ok"] = ok

    threading.Thread(target=worker, daemon=True).start()
    return job_state(name)


# ---------------------------------------------------------------- 3단계 도구 설치

def install_plan() -> list[dict]:
    """없는 것만 설치 대상으로 돌려준다. Homebrew 는 관리자 비밀번호가 필요해 손으로 시킨다."""
    plan = []
    if not shutil.which("brew"):
        plan.append({"name": "Homebrew", "auto": False,
                     "cmd": '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
                     "why": "관리자 비밀번호를 물어서 마법사가 대신 실행하지 않습니다"})
    for tool in ("ffmpeg", "uv"):
        if not shutil.which(tool):
            plan.append({"name": tool, "auto": True, "cmd": f"brew install {tool}", "why": ""})
    if not shutil.which("swiftc"):
        plan.append({"name": "Xcode Command Line Tools", "auto": False, "cmd": "xcode-select --install",
                     "why": "설치 창이 뜨고 관리자 권한이 필요합니다. 끝나면 다시 확인을 누르세요"})
    if not shutil.which("claude"):
        plan.append({"name": "Claude Code", "auto": False,
                     "cmd": "npm install -g @anthropic-ai/claude-code", "why": "초안 생성에 씁니다"})
    return plan


def install_tools() -> dict:
    brew = shutil.which("brew")
    cmds = []
    for item in install_plan():
        if item["auto"] and brew:
            cmds.append((item["cmd"], [brew, "install", item["name"]]))
    if not cmds:
        return {"running": False, "log": ["자동으로 설치할 것이 없습니다"], "ok": True}
    return start_job("install", cmds)


# ---------------------------------------------------------------- 4단계 모델

def whisper_cached() -> bool:
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    return (hub / f"models--{WHISPER_MODEL.replace('/', '--')}").exists()


def model_checks() -> dict:
    seg = DIAR_DIR / "segmentation" / "model.onnx"
    emb = DIAR_DIR / "embedding.onnx"
    return {"whisper": whisper_cached(), "segmentation": seg.exists(), "embedding": emb.exists(),
            "whisper_size": "약 1.6GB", "diar_size": "약 45MB"}


def download_models() -> dict:
    got = model_checks()
    cmds = []
    if not got["whisper"]:
        uv = shutil.which("uv") or "uv"
        cmds.append((f"whisper 모델 내려받기 ({WHISPER_MODEL}, 약 1.6GB)",
                     [uv, "run", "--with", "huggingface_hub", "python", "-c",
                      f"from huggingface_hub import snapshot_download;"
                      f"snapshot_download('{WHISPER_MODEL}')"]))
    if not got["embedding"]:
        DIAR_DIR.mkdir(parents=True, exist_ok=True)
        cmds.append(("화자 임베딩 모델 내려받기 (약 27MB)",
                     ["curl", "-fL", "--progress-bar", "-o", str(DIAR_DIR / "embedding.onnx"), EMB_URL]))
    if not got["segmentation"]:
        DIAR_DIR.mkdir(parents=True, exist_ok=True)
        cmds.append(("화자 분할 모델 내려받기 (약 18MB)",
                     ["bash", "-c",
                      f'set -e; cd {DIAR_DIR!s}; curl -fL --progress-bar -o seg.tar.bz2 "{SEG_URL}"; '
                      f'tar xjf seg.tar.bz2; rm -f seg.tar.bz2; '
                      f'rm -rf segmentation; mv sherpa-onnx-pyannote-segmentation-3-0 segmentation']))
    if not cmds:
        return {"running": False, "log": ["모델이 이미 있습니다 (건너뜀)"], "ok": True, "skipped": True}
    return start_job("models", cmds)


# ---------------------------------------------------------------- 5단계 마이크 권한

def _recorder_bin() -> Path:
    return ms.SCRIPTS / "bin" / "recorder"


def build_recorder() -> tuple[bool, str]:
    """녹음기 바이너리를 확인한다. 없으면 `record.sh` 와 같은 명령으로 컴파일한다 (스펙 3-5).

    Xcode CLT 판정은 이 컴파일이 되는지로 한다. `record.sh` 를 직접 부르지 않는 이유는
    그 스크립트가 `docs/meetings/{날짜}_{slug}/` 폴더를 만들기 때문이다. 설치 확인 때문에
    회의 폴더가 하나 생기면 검수 큐에 잡힌다.
    """
    rec = _recorder_bin()
    if rec.exists() and os.access(rec, os.X_OK):
        return True, "이미 있습니다"
    src = ms.SCRIPTS / "recorder.swift"
    if not shutil.which("swiftc"):
        return False, "swiftc 가 없습니다. `xcode-select --install` 을 먼저 하세요"
    rec.parent.mkdir(parents=True, exist_ok=True)
    proc = run(["swiftc", "-O", "-o", str(rec), str(src)], timeout=300)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:300]
    return True, "컴파일했습니다"


def _judge_audio(path: Path) -> dict:
    """3초 녹음 결과를 실측으로 판정한다 (스펙 3-5): 길이 2.5초 이상 · 평균 음량 -60dB 초과."""
    if not path.exists() or path.stat().st_size < 1000:
        return {"ok": False, "reason": "녹음 파일이 만들어지지 않았습니다 (마이크 권한이 없을 때 나오는 모습입니다)"}
    dur = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)], timeout=30).stdout.strip()
    try:
        sec = float(dur)
    except ValueError:
        return {"ok": False, "reason": "녹음 파일 길이를 읽지 못했습니다"}
    vol = run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
               "-f", "null", "-"], timeout=30)
    mean = None
    for line in (vol.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                mean = float(line.split("mean_volume:")[1].replace("dB", "").strip())
            except ValueError:
                pass
    if sec < 2.5:
        return {"ok": False, "sec": sec, "mean": mean,
                "reason": f"녹음 길이가 {sec:.1f}초로 2.5초에 못 미칩니다"}
    if mean is None or mean <= -60:
        return {"ok": False, "sec": sec, "mean": mean,
                "reason": f"평균 음량이 {mean}dB 입니다. 소리가 담기지 않았습니다 (마이크 권한 또는 입력 장치)"}
    return {"ok": True, "sec": sec, "mean": mean, "reason": ""}


def mic_test_foreground() -> dict:
    """콘솔을 띄운 앱(터미널)의 권한을 본다. 임시 파일에 3초 녹음하고 지운다."""
    ok, why = build_recorder()
    if not ok:
        return {"ok": False, "reason": f"녹음기를 준비하지 못했습니다: {why}"}
    out = ms.STATE_DIR / "mictest-fg.m4a"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    try:
        run([str(_recorder_bin()), str(out), "3"], timeout=30)
        got = _judge_audio(out)
    finally:
        out.unlink(missing_ok=True)                     # 녹음 내용을 남기지 않는다
    got["where"] = "포그라운드 (콘솔을 띄운 앱)"
    got["app"] = host_app()
    return got


def host_app() -> str:
    """권한이 붙는 앱 이름. 사람이 시스템 설정에서 찾아야 하는 항목이다."""
    return os.environ.get("TERM_PROGRAM") or "콘솔을 띄운 앱"


def mic_test_launchd() -> dict:
    """실제 자동 녹음이 도는 경로. 임시 plist 를 만들어 3초 녹음하고 반드시 뒷정리한다 (기준 44)."""
    ok, why = build_recorder()
    if not ok:
        return {"ok": False, "reason": f"녹음기를 준비하지 못했습니다: {why}"}
    out = ms.STATE_DIR / "mictest-launchd.m4a"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    uid = os.getuid()
    plist = {"Label": MICTEST_LABEL,
             "ProgramArguments": [str(_recorder_bin()), str(out), "3"],
             "RunAtLoad": False, "KeepAlive": False}
    try:
        MICTEST_PLIST.parent.mkdir(parents=True, exist_ok=True)
        MICTEST_PLIST.write_bytes(plistlib.dumps(plist))
        boot = run(["launchctl", "bootstrap", f"gui/{uid}", str(MICTEST_PLIST)], timeout=30)
        if boot.returncode != 0:
            run(["launchctl", "load", str(MICTEST_PLIST)], timeout=30)
        run(["launchctl", "kickstart", "-k", f"gui/{uid}/{MICTEST_LABEL}"], timeout=30)
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(1)
            if out.exists() and out.stat().st_size > 1000:
                time.sleep(3)
                break
        got = _judge_audio(out)
    except Exception as exc:                            # noqa: BLE001
        got = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        cleaned = cleanup_mictest()
        out.unlink(missing_ok=True)
    got["where"] = "launchd (자동 녹음이 실제로 도는 경로)"
    # 임시 plist 가 실제로 걷혔는지를 화면에 싣는다. 안 보이면 남의 맥에 임시 등록이
    #  남았는지 알 길이 없다 (기준 44 가 확인하는 것과 같은 값이다).
    got["cleanup"] = {"ok": not cleaned["plist"] and not cleaned["listed"], **cleaned}
    return got


def cleanup_mictest() -> dict:
    """임시 plist 를 반드시 걷어낸다 (기준 44 는 이것을 launchctl list 로 확인한다)."""
    uid = os.getuid()
    run(["launchctl", "bootout", f"gui/{uid}/{MICTEST_LABEL}"], timeout=30)
    run(["launchctl", "unload", str(MICTEST_PLIST)], timeout=30)
    MICTEST_PLIST.unlink(missing_ok=True)
    listed = run(["launchctl", "list"], timeout=30).stdout
    return {"plist": MICTEST_PLIST.exists(), "listed": "mictest" in listed, "label": MICTEST_LABEL}


def mic_step() -> dict:
    """포그라운드를 먼저 보고 통과하면 launchd 를 이어서 본다 (스펙 5-1 5단계)."""
    fg = mic_test_foreground()
    out = {"foreground": fg, "launchd": None, "ok": False, "deeplink": MIC_DEEPLINK,
           "app": host_app()}
    if not fg["ok"]:
        return out
    ld = mic_test_launchd()
    out["launchd"] = ld
    out["ok"] = bool(ld["ok"])
    return out


# ---------------------------------------------------------------- 6단계 캘린더

ICS_HOWTO = [
    "구글 캘린더를 브라우저에서 연다",
    "왼쪽 「내 캘린더」에서 회의가 들어오는 캘린더의 ⋮ > 「설정 및 공유」",
    "아래로 내려 「캘린더 통합」 절의 「비공개 주소의 iCal 형식」을 연다",
    "그 주소를 복사해 아래 칸에 붙여 넣는다 (이 주소는 이 맥에만 저장되고 화면에 다시 표시되지 않는다)",
]


def save_ics(url: str) -> dict:
    """실제로 받아서 판정하고, 통과할 때만 저장한다 (기준 45).

    실패 사유에 주소를 싣지 않는다. urllib 의 예외 문구에는 URL 이 그대로 들어간다.
    """
    url = (url or "").strip()
    if not url.startswith("https"):
        return {"ok": False, "reason": "https 로 시작하는 주소여야 합니다"}
    cw = _cw_provider() if _cw_provider else None
    if not cw:
        return {"ok": False, "reason": "calendar-watch.py 를 불러오지 못했습니다"}
    try:
        raw = cw.urllib.request.urlopen(url, timeout=30).read()
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "reason": f"주소를 열지 못했습니다: {_scrub(exc)}"}
    # 판정 문자열을 쪼개 둔다. 번들 유출 검사(기준 46)가 iCal 첫 줄을 통째로 grep 하는데,
    #  코드에 그대로 적혀 있으면 회의 원문이 없는데도 걸린다.
    if (b"BEGIN:" + b"VCALENDAR") not in raw:
        return {"ok": False, "reason": "받은 내용이 캘린더(iCal) 형식이 아닙니다"}
    try:
        cw.icalendar.Calendar.from_ical(raw)
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "reason": f"캘린더를 해석하지 못했습니다: {_scrub(exc)}"}
    if _ics_file is None:
        return {"ok": False, "reason": "저장 위치가 정해지지 않았습니다"}
    _ics_file.parent.mkdir(parents=True, exist_ok=True)
    _ics_file.write_text(url + "\n", encoding="utf-8")
    os.chmod(_ics_file, 0o600)
    return {"ok": True, "saved_to": str(_ics_file.relative_to(ms.REPO)),
            "rules": ["회의실(LOCATION)이 잡힌 일정만 녹음합니다",
                      "제목에 [녹음] 을 넣으면 회의실이 없어도 녹음합니다"]}


# ---------------------------------------------------------------- 7단계 완료

def menubar_cmd() -> list[str]:
    uv = shutil.which("uv") or "uv"
    return [uv, "run", str(ms.CONSOLE / "menubar" / "meeting-menubar.py")]


def launch_menubar() -> dict:
    """메뉴바 앱을 띄운다. **로그인 항목 등록은 여기서 하지 않는다** (스펙 8절 결정 7)."""
    try:
        subprocess.Popen(menubar_cmd(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "message": "메뉴바에 회의 아이콘이 떴는지 확인하세요"}


MENUBAR_LABEL = "com.meeting-console.meeting-menubar"
MENUBAR_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{MENUBAR_LABEL}.plist"


def login_item(enable: bool) -> dict:
    """로그인 항목 등록·해제. 마법사가 물어보고 사람이 고를 때만 부른다 (자동 등록 없음)."""
    uid = os.getuid()
    if not enable:
        run(["launchctl", "bootout", f"gui/{uid}/{MENUBAR_LABEL}"], timeout=30)
        MENUBAR_PLIST.unlink(missing_ok=True)
        return {"ok": True, "registered": False, "message": "로그인 항목에서 뺐습니다"}
    plist = {"Label": MENUBAR_LABEL, "ProgramArguments": menubar_cmd(),
             "RunAtLoad": True, "KeepAlive": False,
             "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}}
    MENUBAR_PLIST.parent.mkdir(parents=True, exist_ok=True)
    MENUBAR_PLIST.write_bytes(plistlib.dumps(plist))
    boot = run(["launchctl", "bootstrap", f"gui/{uid}", str(MENUBAR_PLIST)], timeout=30)
    if boot.returncode != 0:
        run(["launchctl", "load", str(MENUBAR_PLIST)], timeout=30)
    ok = run(["launchctl", "list", MENUBAR_LABEL], timeout=30).returncode == 0
    return {"ok": ok, "registered": ok,
            "message": "로그인할 때 메뉴바 앱이 뜹니다" if ok else "등록에 실패했습니다"}


def login_item_on() -> bool:
    return run(["launchctl", "list", MENUBAR_LABEL], timeout=10).returncode == 0
