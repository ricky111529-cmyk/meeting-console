#!/usr/bin/env bash
# 미팅 녹음 (Mac 마이크 → 파이프라인 경로에 바로 저장)
#
# 사용법:
#   scripts/record.sh <slug>              # 녹음 시작, Ctrl+C로 종료
#   scripts/record.sh <slug> 2026-08-11   # 날짜 직접 지정
#   scripts/record.sh --list              # 오디오 입력 장치 목록
#
# 입력 장치 변경: 시스템 설정 > 사운드 > 입력 에서 기본 장치를 바꾼다
#
# 결과: docs/meetings/{날짜}_{slug}/audio.m4a  (Git 미추적)
# 종료 후 안내대로 scripts/transcribe.sh 를 실행하면 STT까지 이어집니다.
#
# ⚠️ 녹음 전 참석자 동의를 받으세요.
# ⚠️ 마이크만 녹음됩니다. 온라인 회의 상대방 목소리는 담기지 않습니다.
#    (스피커로 크게 틀어 놓으면 마이크에 섞여 들어가지만 품질이 낮습니다)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 입력 장치는 시스템 기본값을 쓴다 (AVAudioRecorder). 바꾸려면 시스템 설정 > 사운드 > 입력
DURATION=""   # --duration 초. 지정하면 그 시간 뒤 자동 종료 (무인 실행용)

list_devices() {
  # ffmpeg은 장치 목록만 뽑을 때도 비정상 종료 코드를 반환하므로 출력을 먼저 받아둔다
  local raw devs
  raw="$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 || true)"
  devs="$(printf '%s\n' "$raw" | sed -n '/AVFoundation audio devices/,$p' | grep -oE '\[[0-9]+\] .*' || true)"
  echo "오디오 입력 장치:"
  if [ -n "$devs" ]; then
    printf '%s\n' "$devs"
  else
    echo "  (목록 조회 실패)"
  fi
  echo
  echo "녹음은 시스템 기본 입력 장치를 씁니다. 바꾸려면 시스템 설정 > 사운드 > 입력"
}

[ $# -lt 1 ] && { echo "사용법: scripts/record.sh <slug> [YYYY-MM-DD] [--duration 초]" >&2; echo >&2; list_devices >&2; exit 1; }
[ "$1" = "--list" ] && { list_devices; exit 0; }

# --duration 을 먼저 떼어낸다
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --duration) [ $# -ge 2 ] || { echo "--duration 값 없음" >&2; exit 1; }; DURATION="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]}"

SLUG="$1"
DATE="${2:-$(date +%F)}"
[ -n "$DURATION" ] && { [[ "$DURATION" =~ ^[0-9]+$ ]] || { echo "--duration 은 초 단위 정수" >&2; exit 1; }; }

[[ "$SLUG" =~ ^[a-z0-9-]+$ ]] || { echo "slug은 영문 소문자·숫자·하이픈만: $SLUG" >&2; exit 1; }
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "날짜 형식은 YYYY-MM-DD: $DATE" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg 없음: brew install ffmpeg" >&2; exit 1; }

DIR="$REPO/docs/meetings/${DATE}_${SLUG}"
mkdir -p "$DIR"

# 기존 녹음을 절대 덮어쓰지 않는다 (회의 녹음 유실 방지)
AUDIO="$DIR/audio.m4a"
if [ -e "$AUDIO" ]; then
  n=2
  while [ -e "$DIR/audio-$n.m4a" ]; do n=$((n + 1)); done
  AUDIO="$DIR/audio-$n.m4a"
  echo "⚠️ audio.m4a가 이미 있어 audio-$n.m4a 로 저장합니다"
fi

echo "▸ 저장 위치: docs/meetings/${DATE}_${SLUG}/$(basename "$AUDIO")"
echo "▸ 입력 장치: 시스템 기본값 ($(osascript -e 'get name of (get volume settings)' 2>/dev/null || echo '확인 불가'))"
echo "▸ 참석자 동의를 받았는지 확인하세요"
echo
if [ -n "$DURATION" ]; then
  echo "🔴 녹음 중 — $((DURATION / 60))분 후 자동 종료 (Ctrl+C로 조기 종료)"
else
  echo "🔴 녹음 중 — 종료하려면 Ctrl+C"
fi
echo

START_TS=$(date +%s)

# 녹음은 AVAudioRecorder 기반 자체 녹음기로 한다 (2026-08-19 교체).
#  ffmpeg의 avfoundation 직접 캡처는 오디오가 깨져 나왔다. 길이는 맞는데
#  말이 빨리감기처럼 들리고 잡음이 심해 알아들을 수 없었다 (같은 환경에서 17kbps).
#  자동 녹음 4건 전부 STT 불가. 반면 음성 메모·Zoom으로 받은 4건은 전부 정상이었다.
#  (8/18에 44분이 21분으로 줄어든 건 뚜껑을 닫아 시스템이 잠든 것이고, 이 문제와 별개다.)
#  AVAudioRecorder는 음성 메모와 같은 API라 협상을 OS가 처리한다. 소스: scripts/recorder.swift
REC="$REPO/scripts/bin/recorder"
if [ ! -x "$REC" ]; then
  echo "▸ 녹음기 빌드 중"
  swiftc -O -o "$REC" "$REPO/scripts/recorder.swift" || {
    echo "❌ 녹음기 빌드 실패 (Xcode Command Line Tools 필요)" >&2; exit 1; }
fi

# caffeinate: 유휴 절전 진입 방지 (뚜껑 닫힘 절전은 막지 못한다)
STALLED=0
if [ -z "$DURATION" ]; then
  # 대화형: Ctrl+C로 사람이 끝낸다
  caffeinate -i "$REC" "$AUDIO" || true
else
  # 무인 실행: 감시 루프를 둔다 (2026-08-19 추가)
  #  녹음기가 마이크를 못 잡으면 헤더만 쓰고 파일이 안 커지는데도 계속 살아 있었다.
  #  실제로 1시간 56분 동안 28바이트만 쓰고 자동 종료도 안 됐다.
  #  그래서 ①파일 성장 정지 ②예정 시간 초과 두 조건을 스크립트가 직접 감시한다.
  caffeinate -i "$REC" "$AUDIO" "$DURATION" &
  REC_PID=$!
  DEADLINE=$(( START_TS + DURATION + 30 ))
  LAST_SIZE=0
  STALL_SEC=0
  while kill -0 "$REC_PID" 2>/dev/null; do
    sleep 10
    SIZE=$(stat -f%z "$AUDIO" 2>/dev/null || echo 0)
    if [ "$SIZE" -le "$LAST_SIZE" ]; then
      STALL_SEC=$(( STALL_SEC + 10 ))
    else
      STALL_SEC=0
    fi
    LAST_SIZE=$SIZE
    if [ "$STALL_SEC" -ge 60 ]; then
      echo "⚠️ 60초간 파일이 커지지 않았습니다. 마이크를 잡지 못한 것으로 보고 중단합니다."
      echo "   (다른 앱이 마이크를 점유했거나 권한이 막힌 경우입니다)"
      STALLED=1
      # || true 필수: recorder가 이미 끝났으면 kill이 실패하고 set -e가 스크립트를 죽인다
      #  (2026-09-02 15시 회의: 60분 20초로 초과 종료 → 마감 경로와 겹침 → STT 단계 미도달)
      kill -INT "$REC_PID" 2>/dev/null || true
      break
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      echo "▸ 예정 시간에 도달해 종료합니다"
      # || true 필수: recorder가 이미 끝났으면 kill이 실패하고 set -e가 스크립트를 죽인다
      #  (2026-09-02 15시 회의: 60분 20초로 초과 종료 → 마감 경로와 겹침 → STT 단계 미도달)
      kill -INT "$REC_PID" 2>/dev/null || true
      break
    fi
  done
  wait "$REC_PID" 2>/dev/null || true
  # SIGINT 후에도 남아 있으면 확실히 정리한다
  pkill -f "$REC $AUDIO" 2>/dev/null || true
fi

# 마이크를 못 잡은 경우: 쓸모없는 파일을 남기지 않는다
if [ "$STALLED" = "1" ]; then
  SZ=$(stat -f%z "$AUDIO" 2>/dev/null || echo 0)
  if [ "$SZ" -lt 10240 ]; then
    rm -f "$AUDIO"
    rmdir "$DIR" 2>/dev/null || true
    echo "❌ 녹음 실패 — 오디오가 기록되지 않아 파일을 남기지 않았습니다." >&2
    exit 1
  fi
fi

echo
if [ ! -s "$AUDIO" ]; then
  echo "❌ 녹음 파일이 비어 있습니다."
  echo "   마이크 권한을 확인하세요: 시스템 설정 > 개인정보 보호 및 보안 > 마이크"
  echo "   이 스크립트를 실행한 터미널 앱에 권한이 필요합니다."
  rm -f "$AUDIO"
  exit 1
fi

DUR="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$AUDIO" 2>/dev/null | cut -d. -f1 || echo 0)"
ELAPSED=$(( $(date +%s) - START_TS ))

# 녹음 중단 구간 감지: 뚜껑을 닫으면 시스템이 잠들어 그 시간만큼 오디오가 안 들어온다.
#  2026-08-18에 44분 실행 중 21분만 기록됐다 (뚜껑 닫힘).
#  조용히 넘어가면 잘린 녹음으로 노트를 쓰게 되므로 여기서 잡는다.
if [ "${ELAPSED:-0}" -gt 60 ] && [ "${DUR:-0}" -gt 0 ]; then
  RATIO=$(( DUR * 100 / ELAPSED ))
  if [ "$RATIO" -lt 90 ]; then
    echo
    echo "⚠️ 캡처 지연: 경과 $((ELAPSED / 60))분 중 $((DUR / 60))분만 기록됨 (${RATIO}%)"
    echo "   중간에 빠진 구간이 있습니다. 노트를 쓸 때 유실을 전제하세요."
  fi
fi
SIZE="$(du -h "$AUDIO" | cut -f1 | tr -d ' ')"

echo "✅ 녹음 완료: $((DUR / 60))분 ${SIZE}"
echo "   $AUDIO"
echo
# 녹음이 끝나면 STT를 바로 건다 (2026-09-01 추가).
#  전에는 "다음: transcribe.sh ..." 안내만 하고 끝나서 사람이 매번 손으로 돌려야 했다.
#  NO_AUTO_STT=1 로 끌 수 있다.
if [ -n "${NO_AUTO_STT:-}" ]; then
  echo "다음: scripts/transcribe.sh \"$AUDIO\" $SLUG $DATE   (NO_AUTO_STT 로 자동 실행을 껐습니다)"
  exit 0
fi

# 화자 분리를 걸지 말지는 참석자 수로 판단한다.
#  2~3인은 잘 갈리지만(2026-09-01 1on1에서 A 0.77·B 0.83 자동 인식),
#  4인 이상은 신뢰도가 떨어져 틀린 분리본이 오히려 노트를 망친다 (2026-08-25 확인).
SPK_ENV=""
DIA_ENV="NO_DIARIZE=1"
ATT="$DIR/attendees.md"
if [ -f "$ATT" ]; then
  N="$(grep -c '^| ACCEPTED ' "$ATT" 2>/dev/null || echo 0)"
  if [ "$N" -ge 2 ] && [ "$N" -le 3 ]; then
    SPK_ENV="SPEAKERS=$N"
    DIA_ENV=""
    echo "▸ 참석 수락 ${N}명 → 화자 분리를 함께 돌립니다"
  else
    echo "▸ 참석 수락 ${N}명 → 화자 분리는 건너뜁니다 (4인 이상은 신뢰도가 낮습니다)"
  fi
else
  echo "▸ 참석자 기록이 없어 화자 분리는 건너뜁니다"
fi

STT_LOG="$REPO/.claude/calendar-recorder/stt-${DATE}-${SLUG}.log"
echo
echo "▸ STT를 백그라운드로 시작합니다 (로그: ${STT_LOG#$REPO/})"
echo "   진행 상황: tail -f \"$STT_LOG\""

# nice 로 우선순위를 낮춰 다음 회의 녹음이나 다른 작업을 방해하지 않게 한다.
(
  if env $SPK_ENV $DIA_ENV nice -n 10 "$REPO/scripts/transcribe.sh" "$AUDIO" "$SLUG" "$DATE" >"$STT_LOG" 2>&1; then
    WORDS="$(wc -w < "$DIR/transcript.md" 2>/dev/null | tr -d ' ' || echo '?')"
    osascript -e "display notification \"${SLUG} · 약 ${WORDS}단어\" with title \"STT 완료\"" >/dev/null 2>&1 || true
  else
    osascript -e "display notification \"${SLUG} — 로그를 확인하세요\" with title \"STT 실패\"" >/dev/null 2>&1 || true
  fi
) &
disown 2>/dev/null || true
