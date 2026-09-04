# /// script
# requires-python = ">=3.11"
# dependencies = ["sherpa-onnx", "numpy", "soundfile"]
# ///
"""화자 분리 - 오디오를 화자별로 나눠 STT 원문에 라벨을 붙인다.

왜 필요한가: whisper는 "누가 말했는지"를 주지 않는다. 그래서 노트를 쓸 때
발화자를 문맥으로 추측해야 했고, 결정·액션의 담당을 채울 수 없었다.

무엇을 하나: 화자를 SPEAKER_00 / SPEAKER_01 로 나눠준다. **이름까지는 못 붙인다.**
사람이 "SPEAKER_00이 누구"라고 한 번만 알려주면 전체가 이름으로 채워진다.
줄마다 추측하던 것에서 회의당 한 번으로 줄어드는 것이 이 도구의 값이다.

사용법:
  uv run scripts/diarize.py <audio> <vtt> [출력경로] [--speakers N] [--enroll "SPEAKER_00=이름,..."]
    --speakers 생략 시 화자 수를 자동 추정한다. 아는 경우 지정하면 정확도가 오른다.
    --enroll   이번 분리 결과의 화자를 목소리 등록부에 이름으로 저장한다 (1회).
               등록해두면 다음 회의부터 SPEAKER_NN 대신 이름이 자동으로 붙는다.

모델 (scripts/models/diarization/, Git 미추적 45MB):
  segmentation  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
  embedding     https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx

검증 (2026-08-25, 30분 1on1 2인): 화자 2명으로 정확히 갈렸고 특징 발화로 매핑이 확정됐다.
  미확정 블록 6.7%(짧은 맞장구). 겹쳐 말하는 구간은 한쪽으로 몰린다.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODEL_DIR = REPO / "scripts" / "models" / "diarization"
SEG = MODEL_DIR / "segmentation" / "model.onnx"
EMB = MODEL_DIR / "embedding.onnx"
# 목소리 등록부: 이름 → 임베딩 벡터. 등록해두면 SPEAKER_NN 대신 이름이 바로 붙는다.
#  동료 목소리에서 뽑은 개인 데이터이므로 Git 미추적 (.gitignore).
VOICES = REPO / ".claude" / "voice-registry" / "voices.json"


def fail(msg: str) -> "None":
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def parse_vtt(path: Path):
    def ts(t: str) -> float:
        p = t.split(":")
        return float(p[-1]) + 60 * float(p[-2]) + (3600 * float(p[-3]) if len(p) > 2 else 0)

    cues = []
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        lines = [l for l in block.strip().split("\n") if l.strip()]
        if len(lines) >= 2 and "-->" in lines[0]:
            a, b = lines[0].split(" --> ")
            cues.append((ts(a), ts(b), " ".join(lines[1:])))
    return cues


def load_voices() -> dict:
    if VOICES.exists():
        try:
            return json.loads(VOICES.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def speaker_embedding(extractor, wav, segs_for_speaker, sr=16000, max_sec=40.0):
    """한 화자의 구간들을 모아 임베딩 하나를 만든다.

    구간이 짧으면 임베딩이 흔들리므로 최대 40초까지 이어 붙인다.
    """
    import numpy as np
    chunks, total = [], 0.0
    for g in sorted(segs_for_speaker, key=lambda x: x["end"] - x["start"], reverse=True):
        dur = g["end"] - g["start"]
        if dur < 1.0:      # 1초 미만은 맞장구일 가능성이 커서 제외
            continue
        chunks.append(wav[int(g["start"] * sr):int(g["end"] * sr)])
        total += dur
        if total >= max_sec:
            break
    if not chunks:
        return None, 0.0
    audio = np.concatenate(chunks)
    st = extractor.create_stream()
    st.accept_waveform(sample_rate=sr, waveform=audio)
    st.input_finished()
    return extractor.compute(st), total


def cosine(a, b) -> float:
    import numpy as np
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    d = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(a @ b / d)


def main() -> int:
    argv = sys.argv
    skip = set()
    speakers, enroll = 0, ""
    for i, a in enumerate(argv):
        if a == "--speakers" and i + 1 < len(argv):
            speakers = int(argv[i + 1]); skip.update({i, i + 1})
        elif a == "--enroll" and i + 1 < len(argv):
            enroll = argv[i + 1]; skip.update({i, i + 1})
    args = [a for i, a in enumerate(argv[1:], start=1)
            if i not in skip and not a.startswith("--")]

    if len(args) < 2:
        fail("사용법: diarize.py <audio> <vtt> [출력경로] [--speakers N]")
    audio, vtt = Path(args[0]), Path(args[1])
    out = Path(args[2]) if len(args) > 2 else vtt.with_name(vtt.stem.replace("-raw", "") + "-speakers.md")

    for p, name in ((SEG, "segmentation"), (EMB, "embedding")):
        if not p.exists():
            fail(f"{name} 모델 없음: {p.relative_to(REPO)}\n   재다운로드 방법은 이 스크립트 상단 주석 참조")
    if not vtt.exists():
        fail(f"VTT 없음: {vtt} (transcribe.sh가 만듭니다)")

    import numpy as np  # noqa: F401  (soundfile이 필요로 함)
    import soundfile as sf
    import sherpa_onnx

    # whisper 입력과 동일하게 16kHz mono로 맞춘다
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(audio),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav_path],
        check=True,
    )
    wav, sr = sf.read(wav_path, dtype="float32", always_2d=False)
    Path(wav_path).unlink(missing_ok=True)

    # num_clusters를 주면 그 수로 고정, 안 주면 threshold로 자동 추정
    clustering = (
        sherpa_onnx.FastClusteringConfig(num_clusters=speakers)
        if speakers > 0
        else sherpa_onnx.FastClusteringConfig(threshold=0.5)
    )
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG)),
            num_threads=4,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB), num_threads=4),
        clustering=clustering,
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not cfg.validate():
        fail("화자 분리 설정이 유효하지 않습니다")

    mins = len(wav) / sr / 60
    print(f"▸ 화자 분리 중 ({mins:.0f}분, "
          f"{'화자 ' + str(speakers) + '명 지정' if speakers else '화자 수 자동 추정'})")
    segs = [
        {"start": s.start, "end": s.end, "speaker": s.speaker}
        for s in sherpa_onnx.OfflineSpeakerDiarization(cfg).process(wav).sort_by_start_time()
    ]

    # 화자별 임베딩을 뽑아 ①--enroll이면 등록부에 저장 ②아니면 등록부와 대조해 이름을 붙인다
    by_spk = {}
    for g in segs:
        by_spk.setdefault(g["speaker"], []).append(g)
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB), num_threads=4))
    embeds = {}
    for spk, gs in by_spk.items():
        emb, used = speaker_embedding(extractor, wav, gs)
        if emb is not None:
            embeds[spk] = (emb, used)

    names = {}          # speaker index → 사람 이름
    if enroll:
        # "SPEAKER_00=홍길동,SPEAKER_01=김철수" 형태
        reg = load_voices()
        for pair in enroll.split(","):
            if "=" not in pair:
                continue
            key, person = (x.strip() for x in pair.split("=", 1))
            idx = int(key.replace("SPEAKER_", ""))
            if idx not in embeds:
                print(f"⚠️ {key}에 해당하는 화자가 없어 건너뜁니다", file=sys.stderr); continue
            emb, used = embeds[idx]
            reg[person] = {"embedding": [float(x) for x in emb], "seconds": round(used, 1),
                           "source": str(audio.parent.name)}
            names[idx] = person
            print(f"▸ 목소리 등록: {person} ({used:.0f}초 분량)")
        VOICES.parent.mkdir(parents=True, exist_ok=True)
        VOICES.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"   등록부: {VOICES.relative_to(REPO)} (총 {len(reg)}명)")
    else:
        reg = load_voices()
        THRESHOLD = 0.5      # 코사인 유사도. 이보다 낮으면 이름을 붙이지 않는다
        for spk, (emb, _used) in embeds.items():
            best = max(((n, cosine(emb, v["embedding"])) for n, v in reg.items()),
                       key=lambda x: x[1], default=None)
            if best and best[1] >= THRESHOLD:
                names[spk] = best[0]
                print(f"▸ {best[0]} 로 인식 (유사도 {best[1]:.2f})")

    def label_of(spk):
        if spk is None:
            return "(불명)"
        return names.get(spk, f"SPEAKER_{spk:02d}")

    def speaker_at(start: float, end: float):
        mid = (start + end) / 2
        best = None
        for g in segs:
            if g["start"] <= mid <= g["end"]:
                return g["speaker"]
            ov = min(end, g["end"]) - max(start, g["start"])
            if ov > 0 and (best is None or ov > best[1]):
                best = (g["speaker"], ov)
        return best[0] if best else None

    # 같은 화자가 이어지면 한 블록으로 합친다 (줄 단위로 두면 읽기 어렵다)
    blocks, prev = [], None
    for start, end, text in parse_vtt(vtt):
        spk = speaker_at(start, end)
        label = label_of(spk)
        if label == prev and blocks:
            blocks[-1][2] += " " + text
        else:
            blocks.append([label, start, text])
            prev = label

    from collections import Counter
    talk = Counter()
    for g in segs:
        talk[g["speaker"]] += g["end"] - g["start"]
    total = sum(talk.values()) or 1

    lines = [
        f"> 화자 분리본 | {audio.parent.name} | 모델: sherpa-onnx pyannote-segmentation-3.0",
        "> ⚠️ SPEAKER_NN은 **이름이 아니다.** 누가 누구인지는 사람이 확인해야 한다.",
        "> 이름이 붙어 있으면 목소리 등록부(.claude/voice-registry/)와 대조해 자동 인식된 것이다. 틀릴 수 있으니 한 번 훑어볼 것.",
        "> ⚠️ 겹쳐 말하는 구간은 한쪽으로 몰린다. 짧은 맞장구는 (불명)으로 남는다.",
        "",
        "## 발화량",
        "",
        "| 화자 | 발화 시간 | 비율 |",
        "|---|---|---|",
    ]
    for spk, sec in sorted(talk.items()):
        lines.append(f"| {label_of(spk)} | {sec/60:.1f}분 | {sec/total*100:.1f}% |")
    lines += ["", "## 발화", ""]
    for label, start, text in blocks:
        lines.append(f"**[{int(start//60):02d}:{int(start%60):02d}] {label}**: {text}")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    unknown = sum(1 for b in blocks if b[0] == "(불명)")
    print(f"✅ 화자 {len(talk)}명 / 발화 {len(blocks)}블록 (미확정 {unknown}개)")
    print(f"   {out.relative_to(REPO) if out.is_relative_to(REPO) else out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
