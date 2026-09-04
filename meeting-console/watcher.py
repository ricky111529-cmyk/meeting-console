# /// script
# requires-python = ">=3.11"
# ///
"""회의 노트 초안 워처 - 조건을 채운 폴더를 찾아 claude -p 로 초안을 만든다.

launchd 가 5분마다 부른다 (com.meeting-console.meeting-console-watcher).
기존 파이프라인(record.sh · transcribe.sh)을 한 줄도 건드리지 않으려고 폴링으로 발동한다.

사용법:
  uv run watcher.py --once                    한 번 훑고 끝낸다 (launchd 가 쓰는 방식)
  uv run watcher.py --once --dry-run          무엇을 할지만 출력하고 claude 를 부르지 않는다
  uv run watcher.py --once --folder <폴더명>   그 폴더만 본다
  uv run watcher.py --once --folder <폴더명> --force   이미 초안·판정이 있어도 다시 만든다
  uv run watcher.py --once --timeout 600      초안 생성 제한 시간 (기본 1200초 = 20분)
  uv run watcher.py --once --max 3            한 번에 처리할 폴더 수 (기본 1)

만드는 것: docs/meetings/{폴더}/notes.draft.md · review.json · logs/draft-{폴더}.log
notes.md 는 만들지 않는다. 확정은 사람이 콘솔에서 한다.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meeting_state as ms  # noqa: E402

DEFAULT_TIMEOUT = 20 * 60          # 스펙 3-2: 20분. 초과하면 프로세스를 끊는다
CLAUDE_TOOLS = "Read,Glob,Grep,Write"
WATCHER_LOG = ms.LOGS / "watcher.log"


def log(msg: str) -> None:
    line = f"[{datetime.now(ms.TZ):%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    ms.LOGS.mkdir(parents=True, exist_ok=True)
    with WATCHER_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- 대상 판정

def eligible(folder: str, force: bool = False) -> tuple[bool, str]:
    """초안을 만들 대상인지. (대상 여부, 사유)

    조건은 스펙 4절 그대로다. STT 완료 판정이 가장 틀리기 쉬운 곳이라
    mtime 120초와 diarize.py 미실행을 **둘 다** 본다. 하나만 보면 발화자 없는 초안이 나온다.
    """
    d = ms.MEETINGS / folder
    if not d.is_dir():
        return False, "폴더 없음"
    if not (d / "transcript.md").exists():
        return False, "원문 없음 (글로 옮기는 중)"
    if (d / ms.NOTES_NAME).exists():
        return False, "이미 확정된 노트가 있음"
    if ms.lock_info(folder) is not None:
        return False, "노트 작성 중 (락 있음)"
    if not force:
        if (d / ms.DRAFT_NAME).exists():
            return False, "이미 초안이 있음"
        if (d / ms.REVIEW_NAME).exists():
            return False, "이미 판정이 있음 (review.json)"

    pending = ms.stt_in_progress(folder)
    if pending:
        return False, f"STT 안정화 대기 ({pending})"
    age = time.time() - (d / "transcript.md").stat().st_mtime
    if age < ms.STT_SETTLE_SEC:
        return False, f"STT 안정화 대기 (원문이 {int(age)}초 전에 바뀜, {ms.STT_SETTLE_SEC}초 필요)"
    return True, "대상"


# ---------------------------------------------------------------- 프롬프트

def build_prompt(folder: str) -> str:
    d = ms.MEETINGS / folder
    sources = []
    for name, what in (
        ("transcript-speakers.md", "화자 분리본. 있으면 이걸 먼저 읽는다"),
        ("transcript.md", "STT 원문"),
        ("attendees.md", "캘린더 참석자 (초대 명단이며 실제 참석이 아니다)"),
        ("late-start.txt", "녹음이 회의 앞부분을 놓쳤다는 표시"),
    ):
        p = d / name
        if p.exists():
            words = len(p.read_text(encoding="utf-8", errors="replace").split())
            sources.append(f"  - `{name}` ({words:,}단어): {what}")
    if not sources:
        sources.append("  - (없음)")

    text = ms.PROMPT_FILE.read_text(encoding="utf-8")
    return (text
            .replace("{{FOLDER}}", folder)
            .replace("{{DIR}}", f"docs/meetings/{folder}/")
            .replace("{{SOURCES}}", "\n".join(sources))
            .replace("{{DRAFT_PATH}}", f"docs/meetings/{folder}/{ms.DRAFT_NAME}")
            .replace("{{VERDICT_PATH}}", str((ms.VERDICT_DIR / f"{folder}.json"))))


# ---------------------------------------------------------------- 초안 생성

def run_draft(folder: str, timeout: int, dry_run: bool = False) -> dict:
    """claude -p 를 부른다. 결과를 review.json 에 적고 그 내용을 돌려준다."""
    d = ms.MEETINGS / folder
    prompt = build_prompt(folder)
    log_file = ms.LOGS / f"draft-{folder}.log"
    verdict_file = ms.VERDICT_DIR / f"{folder}.json"
    ms.LOGS.mkdir(parents=True, exist_ok=True)
    ms.VERDICT_DIR.mkdir(parents=True, exist_ok=True)
    verdict_file.unlink(missing_ok=True)

    if dry_run:
        log(f"[dry-run] {folder}: claude 를 부르지 않는다")
        return {"status": "dry-run"}

    started = ms.now_iso()
    started_ts = time.time()
    rel_log = str(log_file.relative_to(ms.REPO)) if log_file.is_relative_to(ms.REPO) else str(log_file)

    claude_bin = shutil.which("claude")
    if not claude_bin:
        # 스펙 3-2: claude 가 없는 맥에서는 초안 단계를 통째로 건너뛴다. STT 까지는 정상 동작한다.
        log(f"실패: {folder} - claude 명령을 찾지 못했다 (PATH 확인)")
        # 로그 파일을 실제로 만든다. review.json 에 경로만 적어두면 검수 화면이
        #  없는 파일을 가리켜 "로그 없음"만 뜨고 사람이 원인을 못 본다.
        log_file.write_text(
            f"# 초안 생성 로그 | {folder} | {started}\n"
            "claude 명령을 찾지 못했습니다 (PATH 확인).\n"
            f"PATH={os.environ.get('PATH', '')}\n"
            "설치·로그인 뒤 검수 큐에서 '다시 생성'을 누르면 됩니다.\n", encoding="utf-8")
        return ms.write_review(folder, {
            "status": "failed",
            "reason": "초안 없음 (Claude Code 미설치 또는 PATH에 없음)",
            "decided_at": started,
            "draft": {"started": started, "finished": ms.now_iso(), "exit": 127, "log": rel_log},
        })

    cmd = [claude_bin, "-p", prompt,
           "--allowedTools", CLAUDE_TOOLS,
           "--permission-mode", "acceptEdits"]
    log(f"초안 생성 시작: {folder} (제한 {timeout}초, 로그 {rel_log})")

    timed_out = False
    with log_file.open("w", encoding="utf-8") as fh:
        fh.write(f"# 초안 생성 로그 | {folder} | 시작 {started}\n")
        fh.write(f"# 명령: claude -p <프롬프트 {len(prompt)}자> "
                 f"--allowedTools {CLAUDE_TOOLS} --permission-mode acceptEdits\n\n")
        fh.flush()
        proc = subprocess.Popen(cmd, cwd=str(ms.REPO), stdout=fh,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # 자식이 손자를 낳으므로 프로세스 그룹째 끊는다. 남으면 다음 회차에서 계속 돈다.
            _kill_group(proc)
            code = proc.returncode if proc.returncode is not None else -9
            fh.write(f"\n[watcher] 제한 시간 {timeout}초를 넘겨 종료했습니다\n")

    elapsed = int(time.time() - started_ts)
    draft_rec = {"started": started, "finished": ms.now_iso(), "exit": code,
                 "log": rel_log, "seconds": elapsed}

    if timed_out:
        log(f"실패(시간 초과): {folder} ({timeout}초)")
        return ms.write_review(folder, {
            "status": "failed",
            "reason": f"실패(시간 초과): {timeout}초 제한을 넘겨 프로세스를 종료했다",
            "decided_at": ms.now_iso(), "draft": draft_rec})

    verdict = ms.read_json(verdict_file, None)
    has_draft = (d / ms.DRAFT_NAME).exists()

    # 판정: 종료 코드가 0이 아니거나, 0인데 결과가 없으면 실패다 (스펙 3-2)
    if code != 0:
        log(f"실패: {folder} (exit {code}, {elapsed}초)")
        return ms.write_review(folder, {
            "status": "failed", "reason": f"claude 가 종료 코드 {code} 로 끝났다",
            "decided_at": ms.now_iso(), "draft": draft_rec})

    if verdict and verdict.get("verdict") == "not-internal-meeting":
        # 유형 판정에서 비대상. 초안이 있으면(규칙 위반) 지운다. 개인 대화가 초안으로 남으면 안 된다.
        if has_draft:
            (d / ms.DRAFT_NAME).unlink()
            log(f"비대상인데 초안이 쓰여 있어 삭제: {folder}")
        reason = (verdict.get("reason") or "").strip() or "원문 유형 판정에서 사내 회의가 아니라고 봤다"
        log(f"회의인지 확인: {folder} - {reason}")
        return ms.write_review(folder, {
            "status": "needs-human-check", "reason": reason,
            "decided_at": ms.now_iso(), "draft": draft_rec,
            "verdict": verdict.get("verdict")})

    if not has_draft:
        log(f"실패: {folder} (종료 코드 0 인데 {ms.DRAFT_NAME} 가 없다)")
        return ms.write_review(folder, {
            "status": "failed", "reason": f"{ms.DRAFT_NAME} 가 생성되지 않았다",
            "decided_at": ms.now_iso(), "draft": draft_rec})

    if not verdict:
        # 초안은 있는데 유형 판정 결과가 없다. 자동 방어선이 돌았는지 확인할 수 없으므로
        #  통과시키지 않고 사람에게 넘긴다 (애매하면 사람이 보는 쪽으로 기운다).
        log(f"판정 파일 없음: {folder} - 사람 확인으로 넘긴다")
        return ms.write_review(folder, {
            "status": "needs-human-check",
            "reason": "원문 유형 판정 결과가 없어 자동 확인이 되지 않았다. 초안을 사람이 먼저 볼 것",
            "decided_at": ms.now_iso(), "draft": draft_rec})

    row = verdict.get("index_row") or {}
    log(f"초안 완료: {folder} ({elapsed}초)")
    return ms.write_review(folder, {
        "status": "pending", "reason": (verdict.get("reason") or "").strip(),
        "decided_at": ms.now_iso(), "draft": draft_rec,
        "verdict": verdict.get("verdict"),
        "index_row": {k: str(row.get(k, "")).strip() for k in
                      ("종류", "주제", "관련 프로젝트", "핵심 결정")},
    })


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


# ---------------------------------------------------------------- 한 폴더 처리

def process(folder: str, timeout: int, force: bool, dry_run: bool) -> str:
    ok, why = eligible(folder, force=force)
    if not ok:
        log(f"건너뜀: {folder} - {why}")
        return "skip"

    # 규칙 제외 (스펙 3-4 둘째 겹). 걸리면 claude 를 아예 부르지 않는다.
    hit = ms.match_exclude(folder)
    if hit:
        log(f"규칙 제외: {folder} - {hit}")
        if not dry_run:
            ms.write_review(folder, {"status": "excluded", "reason": hit,
                                     "decided_at": ms.now_iso()})
        return "excluded"

    if not ms.acquire_lock(folder, note="watcher draft"):
        log(f"건너뜀: {folder} - 다른 프로세스가 초안을 만들고 있다 (락)")
        return "skip"
    try:
        res = run_draft(folder, timeout, dry_run=dry_run)
    finally:
        ms.release_lock(folder)
    return res.get("status", "?")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--once", action="store_true", help="한 번 훑고 끝낸다 (기본 동작)")
    ap.add_argument("--folder", help="이 폴더만 본다")
    ap.add_argument("--force", action="store_true", help="이미 초안·판정이 있어도 다시 만든다")
    ap.add_argument("--dry-run", action="store_true", help="claude 를 부르지 않고 대상만 출력")
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get("MEETING_DRAFT_TIMEOUT", DEFAULT_TIMEOUT)),
                    help=f"초안 생성 제한 시간(초). 기본 {DEFAULT_TIMEOUT}")
    ap.add_argument("--max", type=int, default=1, help="한 번에 처리할 폴더 수 (기본 1)")
    ap.add_argument("--no-notify", action="store_true", help="맥 알림을 띄우지 않는다")
    args = ap.parse_args()

    targets = [args.folder] if args.folder else ms.list_folders()
    done = 0
    for folder in targets:
        if done >= args.max:
            log(f"이번 회차 처리 한도({args.max})에 도달해 남은 폴더는 다음 회차로 미룬다")
            break
        result = process(folder, args.timeout, args.force, args.dry_run)
        if result != "skip":
            done += 1

    # 확인 필요 건수를 알린다 (스펙 4절 6번)
    if done and not args.no_notify and not args.dry_run:
        waiting = [m for m in ms.list_meetings() if m["state"] in ("review", "suspect")]
        if waiting:
            ms.notify("회의 콘솔", f"확인 필요 {len(waiting)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
