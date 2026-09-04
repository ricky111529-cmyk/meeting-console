#!/usr/bin/env bash
# 회의 콘솔 설치 (배포 번들에서 실행한다. 스펙 3-6 · 3-7, 수용 기준 48)
#
#   ./install.sh          설치
#   ./install.sh --check  사전 점검만 하고 끝낸다
#
# 자동화되는 것만 여기서 한다. 사람 개입이 필요한 세 가지(마이크 권한 · Xcode CLT ·
# ICS 발급)는 마지막에 뜨는 웹 마법사가 이어받는다. 터미널로 안내하면 어디까지 했는지
# 추적이 안 되고 실패했을 때 돌아갈 데가 없다.
#
# **메뉴바 앱을 로그인 항목으로 자동 등록하지 않는다** (스펙 8절 결정 7). 남의 맥에
# 상주 앱을 말없이 심지 않는다. 마법사 7단계에서 묻는다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONSOLE="$ROOT/meeting-console"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

ok()   { echo "  ✅ $*"; }
no()   { echo "  ❌ $*"; }
step() { echo; echo "▸ $*"; }

# ---------------------------------------------------------------- 1. 사전 점검
step "1/6 사전 점검"
FAIL=0

MACOS="$(sw_vers -productVersion 2>/dev/null || echo "?")"
case "$MACOS" in
  1[3-9].*|2[0-9].*) ok "macOS $MACOS" ;;
  *) no "macOS $MACOS (13 이상이 필요합니다)"; FAIL=1 ;;
esac

ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then ok "Apple Silicon ($ARCH)"
else no "Apple Silicon 이 아닙니다 ($ARCH). whisper·화자 분리가 MLX 를 써서 여기서 멈춥니다"; FAIL=1; fi

FREE_GB="$(df -g "$HOME" | awk 'NR==2 {print $4}')"
if [ "${FREE_GB:-0}" -ge 3 ]; then ok "디스크 여유 ${FREE_GB}GB"
else no "디스크 여유 ${FREE_GB}GB (3GB 이상 필요)"; FAIL=1; fi

for f in scripts/record.sh scripts/transcribe.sh scripts/diarize.py scripts/calendar-watch.py \
         meeting-console/server.py meeting-console/menubar/meeting-menubar.py; do
  if [ -e "$ROOT/$f" ]; then ok "$f"
  else no "$f 가 번들에 없습니다"; FAIL=1; fi
done

# 부재의 뜻이 도구마다 다르다. 2단계가 직접 깔아 주는 것과, 사람이 먼저 해야 하는 것을 가른다.
#  전부 경고로만 두면 swiftc·claude 가 없는 채로 설치가 끝나고 첫 녹음에서야 막힌다.
for t in ffmpeg uv; do
  if command -v "$t" >/dev/null; then ok "$t $(command -v "$t")"
  else echo "   · $t 없음 (2단계에서 설치합니다)"; fi
done
if command -v brew >/dev/null; then ok "brew $(command -v brew)"
elif command -v ffmpeg >/dev/null; then echo "   · brew 없음 (ffmpeg 가 이미 있어 필요 없습니다)"
else no "brew 없음. ffmpeg 를 설치할 방법이 없습니다: https://brew.sh"; FAIL=1; fi
if command -v swiftc >/dev/null; then ok "swiftc $(command -v swiftc)"
else no "swiftc 없음. 녹음기를 빌드할 수 없습니다. \`xcode-select --install\` 로 커맨드라인 도구를 먼저 설치하세요"; FAIL=1; fi
if command -v claude >/dev/null; then ok "claude $(command -v claude)"
else no "claude 없음. 초안 생성이 돌지 않습니다. Claude Code 를 먼저 설치하세요"; FAIL=1; fi

if [ "$FAIL" != "0" ]; then
  echo; echo "❌ 사전 점검에서 막혔습니다. 위 항목을 해결하고 다시 실행하세요." >&2
  exit 1
fi
if [ "$CHECK_ONLY" = "1" ]; then
  echo; echo "사전 점검까지만 돌렸습니다 (--check). 설치하려면 인자 없이 다시 실행하세요."
  exit 0
fi

# ---------------------------------------------------------------- 2. 도구
step "2/6 도구 설치 (없는 것만)"
if ! command -v brew >/dev/null; then
  echo "  Homebrew 가 없습니다. 관리자 비밀번호를 묻기 때문에 여기서 대신 설치하지 않습니다."
  echo "  아래를 직접 실행한 뒤 install.sh 를 다시 실행하세요:"
  echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 1
fi
for t in ffmpeg uv; do
  command -v "$t" >/dev/null && { ok "$t 있음"; continue; }
  echo "  brew install $t"; brew install "$t"
done
command -v swiftc >/dev/null || {
  echo "  Xcode Command Line Tools 가 없습니다. 설치 창을 띄웁니다 (끝나면 다시 실행하세요)."
  xcode-select --install || true
  exit 1
}

# ---------------------------------------------------------------- 3. 모델
step "3/6 모델 내려받기"
MODEL_DIR="$ROOT/scripts/models/diarization"
mkdir -p "$MODEL_DIR"
if [ -d "$HOME/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo" ]; then
  ok "whisper large-v3-turbo 있음 (건너뜀)"
else
  echo "  whisper large-v3-turbo 내려받기 (약 1.6GB)"
  uv run --with huggingface_hub python -c \
    "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/whisper-large-v3-turbo')"
fi
if [ -f "$MODEL_DIR/embedding.onnx" ]; then ok "화자 임베딩 모델 있음 (건너뜀)"
else
  echo "  화자 임베딩 모델 내려받기 (약 27MB)"
  curl -fL --progress-bar -o "$MODEL_DIR/embedding.onnx" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
fi
if [ -f "$MODEL_DIR/segmentation/model.onnx" ]; then ok "화자 분할 모델 있음 (건너뜀)"
else
  echo "  화자 분할 모델 내려받기 (약 18MB)"
  ( cd "$MODEL_DIR" \
    && curl -fL --progress-bar -o seg.tar.bz2 \
       "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2" \
    && tar xjf seg.tar.bz2 && rm -f seg.tar.bz2 \
    && rm -rf segmentation && mv sherpa-onnx-pyannote-segmentation-3-0 segmentation )
fi

# ---------------------------------------------------------------- 4. 녹음기 컴파일
step "4/6 녹음기 컴파일"
mkdir -p "$ROOT/scripts/bin"
if [ -x "$ROOT/scripts/bin/recorder" ]; then ok "이미 있습니다"
else
  swiftc -O -o "$ROOT/scripts/bin/recorder" "$ROOT/scripts/recorder.swift"
  ok "scripts/bin/recorder"
fi

# ---------------------------------------------------------------- 5. launchd
step "5/6 자동 녹음 등록 (launchd)"
# 번들의 plist 는 템플릿이다. 경로를 여기서 채운다 (하드코딩된 plist 를 나눠주지 않는다).
UV="$(command -v uv)"
UVBIN="$(dirname "$UV")"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA" "$ROOT/.claude/calendar-recorder"

sed -e "s|__UV__|$UV|g" -e "s|__UVBIN__|$UVBIN|g" -e "s|__REPO__|$ROOT|g" \
    "$ROOT/scripts/com.meeting-console.meeting-recorder.plist.template" \
    > "$LA/com.meeting-console.meeting-recorder.plist"
# claude 명령이 PATH 밖(npm 전역 등)에 있으면 launchd 워처가 못 찾아 초안 단계가 통째로 실패한다 (2026-09-04 실측: ~/.npm-global/bin)
CLAUDE_BIN_DIR="$(dirname "$(command -v claude 2>/dev/null || echo /usr/local/bin/claude)")"
sed -e "s|__UV__|$UV|g" -e "s|__CONSOLE__|$CONSOLE|g" -e "s|__REPO__|$ROOT|g" \
    -e "s|__PATH__|$UVBIN:$CLAUDE_BIN_DIR:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin|g" \
    "$ROOT/scripts/com.meeting-console.meeting-console-watcher.plist.template" \
    > "$LA/com.meeting-console.meeting-console-watcher.plist"

for label in com.meeting-console.meeting-recorder com.meeting-console.meeting-console-watcher; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/$label.plist" 2>/dev/null \
    || launchctl load "$LA/$label.plist" 2>/dev/null || true
  if launchctl list "$label" >/dev/null 2>&1; then ok "$label"; else no "$label 등록 실패"; fi
done
echo "  캘린더 주소를 아직 넣지 않았으므로 자동 녹음은 마법사 6단계 뒤부터 동작합니다."

# ---------------------------------------------------------------- 6. 마법사
step "6/6 설치 마법사 열기"
echo "  브라우저가 열리면 마이크 권한 · 캘린더 연결 · 메뉴바 등록을 이어서 합니다."
echo "  회의 폴더는 $ROOT/docs/meetings/ 아래에 생깁니다"
echo "  (이 이름은 녹음·STT·노트 스크립트가 공유하는 경로라 바꾸지 않습니다)."
echo
exec uv run "$CONSOLE/server.py" --open-setup
