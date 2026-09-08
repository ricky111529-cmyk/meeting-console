#!/usr/bin/env python3
"""회의가 끝난 뒤 이어진 빈 녹음(무음 환각 구간)을 오디오·원문 뒤에서 잘라낸다.

  uv run scripts/trim-tail.py <audio> <vtt> [transcript.md] [--dry-run] [--min-tail 180]

whisper 는 무음에서 "감사합니다", "다음 영상에서 만나요", 같은 짧은 문장을 30~60초마다 뱉는다.
그 구간을 남겨 두고 화자 분리를 N명으로 강제하면 소음이 덩어리 하나를 차지해 실제 두 사람이
합쳐진다 (2026-09-08 15:00 회의 실사고: 28분 빈 녹음 → 3명 중 2명 병합. 잘라내니 정상 분리).

판정: 뒤에서부터 "환각 같은 큐"(짧다 · 알려진 환각 문구 · 직전과 같은 문장)만 이어지는 구간을 찾고,
그 구간이 --min-tail 초 이상이며 그 안의 발화 밀도가 낮을 때만 자른다. 마지막 실제 발화 끝 + 15초에서 끊는다.
원본은 original-untrimmed.<ext>.bak 으로 남긴다. 판단이 애매하면 자르지 않는다 (안 자르는 쪽이 안전).
"""
from __future__ import annotations
import re, shutil, subprocess, sys
from pathlib import Path

KNOWN = ("다음 영상에서 만나요", "시청해 주셔서", "시청해주셔서", "감사합니다", "이 시각 세계", "자막 제공",
         "구독", "좋아요", "고춧가루", "고기", "!", "아", "네", "음", "MBC", "KBS", "SBS", "뉴스")
PAD_SEC = 15.0


def secs(t: str) -> float:
    p = [float(x) for x in t.strip().split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


def parse(vtt: str) -> tuple[list[str], list[dict]]:
    blocks = re.split(r"\n\s*\n", vtt.strip())
    header, cues = [], []
    for b in blocks:
        lines = b.strip().splitlines()
        ts = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts is None:
            header.append(b); continue
        a, z = lines[ts].split("-->")
        text = " ".join(l.strip() for l in lines[ts + 1:]).strip()
        cues.append({"start": secs(a), "end": secs(z.split()[0]), "text": text, "raw": b})
    return header, cues


def hallucination_like(c: dict, prev_text: str, counts: dict | None = None, rep_words: list | None = None) -> bool:
    t = re.sub(r"[\s.。!?…~]+", "", c["text"])
    if counts and counts.get(t, 0) >= 3:            # 같은 문장이 파일 안에 3번 이상 = 반복 붕괴 ("대형에 대형에…")
        return True
    if rep_words:                                    # 반복 문장의 단어만으로 된 변형 ("대형에 대형을 사용하여")
        words = set(c["text"].split())
        if words and any(words <= w for w in rep_words):
            return True
    if len(t) <= 6:
        return True
    if any(k.replace(" ", "") in t for k in KNOWN) and len(t) <= 16:
        return True
    if c["text"] == prev_text:
        return True
    return False


def minute_levels(audio: Path) -> list[float]:
    """분 단위 평균 음량(dBFS). ffmpeg 로 8kHz 모노 PCM 을 받아 RMS 를 낸다. 실패하면 빈 목록."""
    import array, math
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                           capture_output=True, timeout=600)
    except Exception:
        return []
    if r.returncode != 0 or not r.stdout:
        return []
    a = array.array("h"); a.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    per = 8000 * 60
    out = []
    for i in range(0, len(a), per):
        chunk = a[i:i + per]
        if len(chunk) < per // 4:
            break
        rms = math.sqrt(sum(x * x for x in chunk) / len(chunk)) / 32768.0
        out.append(20 * math.log10(rms) if rms > 0 else -100.0)
    return out


def quiet_tail_start(levels: list[float], drop_db: float = 6.0) -> tuple[float | None, str]:
    """끝까지 이어지는 조용한 구간의 시작(초). 기준은 앞 절반의 중앙값 - drop_db."""
    if len(levels) < 8:
        return None, "오디오가 짧아 음량 판정 생략"
    head = sorted(levels[: max(4, len(levels) // 2)])
    base = head[len(head) // 2]
    thr = base - drop_db
    # 뒤에서부터 넓혀 가며, 그 구간의 80% 이상이 조용하면 조용한 구간으로 본다.
    #  문 닫는 소리 같은 1분짜리 튐(2026-09-08 44분 max 0dB)에 끊기지 않게 하려는 것
    n = len(levels)
    best = n
    quiet = 0
    for m in range(n - 1, -1, -1):
        if levels[m] < thr:
            quiet += 1
        span = n - m
        if levels[m] < thr and quiet / span >= 0.8:
            best = m
        elif span >= 5 and quiet / span < 0.6:
            break
    m = best
    if m == n:
        return None, f"끝부분 음량이 회의 중과 같다 (기준 {base:.1f}dB)"
    return m * 60.0, f"음량: 회의 중 중앙값 {base:.1f}dB, {m}분부터 끝까지 {thr:.1f}dB 미만"


def find_cut(cues: list[dict], min_tail: float) -> tuple[float | None, str]:
    if len(cues) < 10:
        return None, "큐가 너무 적다"
    total_end = cues[-1]["end"]
    counts: dict = {}
    for c in cues:
        k = re.sub(r"[\s.。!?…~]+", "", c["text"])
        counts[k] = counts.get(k, 0) + 1
    rep_words = [set(c["text"].split()) for c in cues
                 if counts.get(re.sub(r"[\s.。!?…~]+", "", c["text"]), 0) >= 3 and c["text"].split()]
    def h(j):
        return hallucination_like(cues[j], cues[j - 1]["text"] if j else "", counts, rep_words)
    i = len(cues) - 1
    while i >= 0:
        if h(i):
            i -= 1
            continue
        # 환각 구간 속에 낀 이상 큐 하나는 건너뛴다: 앞의 세 큐가 모두 환각 같으면 이것도 환각으로 본다
        if i >= 3 and all(h(j) for j in range(i - 3, i)):
            i -= 1
            continue
        break
    # cues[i] = 마지막 실제 발화. 그 뒤가 전부 환각 같은 큐
    if i < 0:
        return None, "전체가 환각 같다 (판단 보류)"
    tail_start = cues[i]["end"]
    tail_len = total_end - tail_start
    if tail_len < min_tail:
        return None, f"뒤쪽 빈 구간 {tail_len:.0f}초 (< {min_tail:.0f}초, 자르지 않음)"
    tail_speech = sum(c["end"] - c["start"] for c in cues[i + 1:])
    density = tail_speech / tail_len if tail_len else 1
    if density > 0.35:
        return None, f"뒤쪽 {tail_len:.0f}초의 발화 밀도 {density:.2f} 가 높아 실제 대화일 수 있다 (자르지 않음)"
    return tail_start + PAD_SEC, f"마지막 실제 발화 {tail_start:.0f}초, 뒤쪽 빈 구간 {tail_len:.0f}초 (큐 {len(cues) - i - 1}개, 밀도 {density:.2f})"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    min_tail = 180.0
    if "--min-tail" in sys.argv:
        min_tail = float(sys.argv[sys.argv.index("--min-tail") + 1])
    if len(args) < 2:
        print(__doc__); return 2
    audio, vtt_path = Path(args[0]), Path(args[1])
    transcript = Path(args[2]) if len(args) > 2 else None
    header, cues = parse(vtt_path.read_text(encoding="utf-8"))
    text_cut, why = find_cut(cues, min_tail)
    # 음량으로 보강한다. 빈 회의실 소음은 회의 중보다 6dB 이상 낮다 (2026-09-08 실측: -31 → -40dB).
    #  텍스트만 보면 소음 위에 얹힌 조각 문장("이렇게, 자유롭게 볼 수 있는,")을 실제 말로 오해한다.
    energy_cut, ewhy = (None, "오디오 없음")
    if audio.exists():
        levels = minute_levels(audio)
        q, ewhy = quiet_tail_start(levels)
        if q is not None and (cues[-1]["end"] - q) >= min_tail:
            # 조용한 구간 시작 직전의 마지막 큐 끝 + 여유. 그 구간 안의 큐는 소음 위 환각으로 본다
            last_real = max([c["end"] for c in cues if c["end"] <= q + 30], default=q)
            energy_cut = last_real + PAD_SEC
    cands = [c for c in (text_cut, energy_cut) if c is not None]
    if not cands:
        print(f"▸ 뒤쪽 자르기 없음: {why} / {ewhy}"); return 0
    cut = min(cands)
    why = f"{why} / {ewhy}"
    keep = [c for c in cues if c["start"] < cut]
    print(f"▸ 뒤쪽 빈 녹음 감지: {why} → {cut:.0f}초에서 자른다 (큐 {len(cues) - len(keep)}개 제거)")
    if dry:
        return 0
    # 오디오: 원본을 .bak 로 옮기고 무손실로 자른다. 이름이 audio 로 시작하지 않아야 콘솔이 오디오로 안 잡는다
    bak = audio.with_name(f"original-untrimmed{audio.suffix}.bak")
    if not bak.exists():
        shutil.move(str(audio), str(bak))
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(bak), "-t", f"{cut:.3f}", "-c", "copy", str(audio)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not audio.exists() or audio.stat().st_size == 0:
        shutil.move(str(bak), str(audio))
        print(f"⚠️ 오디오 자르기 실패, 원본 유지: {r.stderr.strip()[:200]}"); return 0
    vtt_bak = vtt_path.with_name("original-untrimmed.vtt.bak")
    if not vtt_bak.exists():
        shutil.copy2(vtt_path, vtt_bak)
    vtt_path.write_text("\n\n".join(header + [c["raw"] for c in keep]) + "\n", encoding="utf-8")
    if transcript and transcript.exists():
        lines = transcript.read_text(encoding="utf-8").splitlines()
        head = []
        for l in lines:
            if l.startswith(">") or l.strip() == "":
                head.append(l)
            else:
                break
        body = [c["text"] for c in keep if c["text"]]
        transcript.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    print(f"   원본 보존: {bak.name} · {vtt_bak.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
