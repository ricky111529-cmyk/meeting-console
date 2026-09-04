#!/usr/bin/env bash
# 녹음 → STT 원문 변환
#
# 사용법:
#   scripts/transcribe.sh <오디오파일> <slug> [YYYY-MM-DD]
#   scripts/transcribe.sh ~/Downloads/rec.m4a aip-weekly
#   scripts/transcribe.sh --latest aip-weekly 2026-08-11
#
# 결과(기본): docs/meetings/{날짜}_{slug}/ 에 audio + transcript.md 생성
#             둘 다 .gitignore 대상. Git에 올라가는 건 /meeting이 만드는 notes.md 뿐.
#
# 사내 미팅이 아닌 녹음(유저 인터뷰 등)은 --out 으로 저장 위치를 직접 지정한다.
#   --out <경로.md>  : 결과를 그 파일로 저장하고 원본 오디오는 복사하지 않는다
#                      (Zoom 녹화처럼 원본이 이미 다른 곳에 보관돼 있을 때)
#
# 화자 수를 알면 SPEAKERS=2 를 붙이면 화자 분리 정확도가 오른다 (모르면 자동 추정).
#   예: SPEAKERS=3 scripts/transcribe.sh --latest aip-weekly
# 화자 분리를 아예 끄려면 NO_DIARIZE=1 (참석자가 많은 회의는 분리가 잘 안 된다).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL="${WHISPER_MODEL:-mlx-community/whisper-large-v3-turbo}"
OUT_FILE=""

usage() {
  echo "사용법: scripts/transcribe.sh <오디오파일|--latest> <slug> [YYYY-MM-DD] [--out 경로.md]" >&2
  echo "  --latest : 바탕화면·다운로드·동영상·음악에서 가장 최근 오디오를 자동으로 찾음" >&2
  echo "             (음성 메모 앱 내부는 macOS가 차단하므로 먼저 바탕화면으로 드래그)" >&2
  echo "  --out    : 사내 미팅이 아닌 녹음의 저장 경로 (오디오 복사 안 함)" >&2
  exit 1
}

# --out 을 먼저 떼어낸다 (위치 인자 순서에 영향 주지 않도록)
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --out) [ $# -ge 2 ] || usage; OUT_FILE="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]}"

[ $# -lt 2 ] && usage
SRC="$1"
SLUG="$2"
DATE="${3:-$(date +%F)}"

# --latest: 가장 최근 오디오 파일 자동 탐색 (숨은 폴더 경로를 몰라도 되게)
if [ "$SRC" = "--latest" ]; then
  echo "▸ 최근 녹음 파일 탐색 중"
  # 각 단계가 비정상 종료해도(없는 폴더, SIGPIPE) 파이프 전체가 죽지 않게 감싼다
  SRC="$( { find \
      "$HOME/Library/Group Containers/group.com.apple.VoiceMemos.shared" \
      "$HOME/Desktop" "$HOME/Downloads" "$HOME/Movies" \
      -type f \( -iname '*.m4a' -o -iname '*.wav' -o -iname '*.mp3' \
                 -o -iname '*.aifc' -o -iname '*.aiff' -o -iname '*.mp4' \) \
      -not -name '.*' -print0 2>/dev/null || true; } \
    | { xargs -0 stat -f '%m %N' 2>/dev/null || true; } \
    | { sort -rn || true; } | { head -1 || true; } | cut -d' ' -f2- || true)"
  [ -n "$SRC" ] || { echo "오디오 파일을 찾지 못했습니다. 경로를 직접 지정하세요." >&2; exit 1; }
  echo "▸ 찾은 파일: $SRC"
  echo "▸ 녹음 시각: $(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$SRC")"
  echo "  (원하는 파일이 아니면 Ctrl+C 후 경로를 직접 지정하세요)"
  echo
fi

[ -f "$SRC" ] || { echo "오디오 파일 없음: $SRC" >&2; exit 1; }
[[ "$SLUG" =~ ^[a-z0-9-]+$ ]] || { echo "slug은 영문 소문자·숫자·하이픈만: $SLUG" >&2; exit 1; }
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "날짜 형식은 YYYY-MM-DD: $DATE" >&2; exit 1; }

command -v ffmpeg >/dev/null || { echo "ffmpeg 없음: brew install ffmpeg" >&2; exit 1; }
WHISPER="$(command -v mlx_whisper || true)"
[ -n "$WHISPER" ] || { echo "mlx_whisper 없음: uv tool install mlx-whisper" >&2; exit 1; }

if [ -n "$OUT_FILE" ]; then
  # --out 모드: 지정 파일에 쓰고 오디오는 원래 자리에 둔다
  case "$OUT_FILE" in /*) ;; *) OUT_FILE="$REPO/$OUT_FILE" ;; esac
  DIR="$(dirname "$OUT_FILE")"
  mkdir -p "$DIR"
  TRANSCRIPT="$OUT_FILE"
  AUDIO="$SRC"
  [ -e "$TRANSCRIPT" ] && { echo "이미 있는 파일입니다. 덮어쓰지 않습니다: $TRANSCRIPT" >&2; exit 1; }
else
  DIR="$REPO/docs/meetings/${DATE}_${SLUG}"
  mkdir -p "$DIR"
  TRANSCRIPT="$DIR/transcript.md"

  # 원본 보존 (확장자 유지). 이미 폴더 안 파일이면 복사 생략
  EXT="${SRC##*.}"
  AUDIO="$DIR/audio.$EXT"
  if [ "$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")" != "$AUDIO" ]; then
    cp "$SRC" "$AUDIO"
  fi
fi

# whisper 입력용 16kHz mono wav (임시)
WAV="$(mktemp -t stt).wav"
trap 'rm -f "$WAV"' EXIT

# 전처리: 16kHz mono + 음량 보정
#  회의실 녹음은 마이크가 멀어 평균 -35dB 이하로 작게 잡히는 경우가 많다.
#  조용한 구간에서 whisper가 같은 토큰을 무한 반복하므로 음량을 먼저 끌어올린다.
#  highpass=저역 잡음 제거 / speechnorm=음성 구간 정규화 / alimiter=피크 클리핑 방지
echo "▸ 전처리: 16kHz mono 변환 + 음량 보정"
BEFORE="$(ffmpeg -hide_banner -i "$AUDIO" -af volumedetect -f null /dev/null 2>&1 | grep -oE 'mean_volume: [-0-9.]+ dB' | head -1 || true)"
ffmpeg -loglevel error -y -i "$AUDIO" \
  -af "highpass=f=80,speechnorm=e=12.5:r=0.0001:l=1,alimiter=limit=0.95" \
  -ac 1 -ar 16000 -c:a pcm_s16le "$WAV"
AFTER="$(ffmpeg -hide_banner -i "$WAV" -af volumedetect -f null /dev/null 2>&1 | grep -oE 'mean_volume: [-0-9.]+ dB' | head -1 || true)"
echo "   음량: ${BEFORE:-?} → ${AFTER:-?}"

DUR="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$WAV" 2>/dev/null | cut -d. -f1 || echo 0)"
echo "▸ 길이: $((DUR / 60))분 ${DUR}초 / 모델: $MODEL"
echo "▸ STT 변환 중 (1시간 음성 기준 수 분 소요)"

OUT="$(mktemp -d)"
trap 'rm -f "$WAV"; rm -rf "$OUT"' EXIT

# --initial-prompt(어휘 힌트)는 쓰지 않는다.
#  10초 샘플에서는 제품명을 비슷한 발음의 다른 단어로 듣는 문제를 고쳐줬지만, 실제 64분 회의 녹음에서는
#  같은 문장을 2,729회 반복하는 붕괴를 일으켰다. 자연문으로 바꿔도 마찬가지였다.
#  고유명사 보정은 STT 단계가 아니라 /meeting 노트 작성 단계에서 vocab.txt로 처리한다.
#
# --condition-on-previous-text False: 앞 구간 출력을 다음 구간 조건으로 물려주지 않아
#  반복 루프가 뒤로 전파되지 않는다. 회의 녹음처럼 침묵이 긴 오디오에 필수.
#
# 출력은 all(txt·vtt·srt·tsv·json). txt는 읽기용, vtt는 타임스탬프용으로 쓴다.
#  타임스탬프가 없으면 오인식 구간을 원본 오디오에서 되찾을 수 없고,
#  교정본의 구간 표기(00:14~00:16)도 만들 수 없다.
#  --output-format 은 쉼표 목록을 받지 않는다 (txt,vtt → invalid choice 에러).
"$WHISPER" "$WAV" \
  --model "$MODEL" \
  --language ko \
  --output-format all \
  --output-dir "$OUT" \
  --word-timestamps False \
  --condition-on-previous-text False

TXT="$(find "$OUT" -name '*.txt' | awk 'NR==1')"   # head 대신 awk (위 164줄과 같은 SIGPIPE 이유)
[ -n "$TXT" ] || { echo "STT 결과 없음" >&2; exit 1; }
VTT="$(find "$OUT" -name '*.vtt' | head -1 || true)"

{
  echo "> STT 원문 | ${DATE} | ${SLUG} | 모델: ${MODEL}"
  echo "> ⚠️ 미검수 자동 변환본. 고유명사·수치는 반드시 원본 오디오로 확인할 것."
  echo "> ⚠️ 이 파일에는 화자 구분이 없다. 화자별로 나뉜 것은 같은 폴더의 *-speakers.md 를 볼 것."
  echo "> ⚠️ 외부 공유 금지."
  [ -n "$OUT_FILE" ] && echo "> 원본 오디오: ${AUDIO}"
  echo
  cat "$TXT"
} > "$TRANSCRIPT"

WORDS="$(wc -w < "$TRANSCRIPT" | tr -d ' ')"

# 타임스탬프본 (원본 오디오 구간 되찾기용)
TS_FILE="${TRANSCRIPT%.md}.vtt"
if [ -n "${VTT:-}" ]; then cp "$VTT" "$TS_FILE"; fi

# 품질 검사: 반복 붕괴 감지
#  whisper가 무너지면 같은 문장만 수천 번 뱉는다. 조용히 넘어가면 쓰레기 원문으로
#  노트를 쓰게 되므로 여기서 잡는다.
LINES="$(grep -c . "$TXT" || echo 0)"
# head로 자르면 앞단 sort가 SIGPIPE(141)를 받고 pipefail이 그걸 스크립트 실패로 만든다
#  (2026-08-26, 90분 원문에서 실제로 터졌다). awk로 첫 줄만 집으면 입력을 끝까지 읽어 안전하다.
TOPDUP="$(sort "$TXT" | uniq -c | sort -rn | awk 'NR==1{print $1}')"
if [ "${LINES:-0}" -gt 20 ] && [ "${TOPDUP:-0}" -gt 0 ]; then
  RATIO=$((TOPDUP * 100 / LINES))
  if [ "$RATIO" -ge 30 ]; then
    echo
    echo "⚠️ 반복 붕괴 의심: 한 문장이 전체의 ${RATIO}% (${TOPDUP}/${LINES}줄)"
    echo "   원본 음량이 너무 작을 때 발생합니다. transcript.md를 먼저 열어 확인하세요."
    echo "   노트 작성을 진행하면 안 됩니다."
  fi
fi

# 화자 분리 (모델이 있을 때만. 없으면 조용히 건너뛴다)
#  whisper는 "누가 말했는지"를 주지 않아 노트에서 발화자를 추측해야 했다.
#  분리본이 있으면 /meeting이 그걸 읽어 결정·액션에 발화자를 근거 있게 넣는다.
#  이름 매핑은 여전히 사람 몫이다 (SPEAKER_00이 누구인지 한 번만 알려주면 전체가 채워진다).
#  ⚠️ 신뢰도가 회의마다 크게 갈린다 (2026-08-25 확인). 참석자가 많거나 결과가 의심스러우면
#     NO_DIARIZE=1 로 끈다. 틀린 분리본은 없느니만 못하다.
SPK_OUT="${TRANSCRIPT%.md}-speakers.md"
if [ -n "${NO_DIARIZE:-}" ]; then
  :   # 화자 분리 건너뜀 (NO_DIARIZE)
elif [ -f "$REPO/scripts/models/diarization/embedding.onnx" ] && [ -f "${TS_FILE:-}" ]; then
  uv run --quiet "$REPO/scripts/diarize.py" "$AUDIO" "$TS_FILE" "$SPK_OUT" ${SPEAKERS:+--speakers "$SPEAKERS"} || {
    echo "⚠️ 화자 분리 실패 (원문은 정상). 수동 실행: uv run scripts/diarize.py \"$AUDIO\" \"$TS_FILE\"" >&2
  }
fi

echo
echo "✅ 완료"
if [ -n "$OUT_FILE" ]; then
  echo "   원본:   ${AUDIO} (그 자리에 그대로 둠)"
  echo "   원문:   ${TRANSCRIPT#$REPO/} (약 ${WORDS} 단어)"
  echo
  echo "사내 미팅이 아니므로 /meeting 대상이 아닙니다. 후속 처리는 해당 폴더 규칙을 따르세요."
else
  echo "   원본:   docs/meetings/${DATE}_${SLUG}/audio.$EXT"
  echo "   원문:   docs/meetings/${DATE}_${SLUG}/transcript.md (약 ${WORDS} 단어)"
  [ -f "$SPK_OUT" ] && echo "   화자별: docs/meetings/${DATE}_${SLUG}/$(basename "$SPK_OUT")"
  echo
  echo "다음: Claude에서  /meeting ${DATE}_${SLUG}"
fi
