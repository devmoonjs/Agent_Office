# install.ps1 — Agent Office 윈도우(WSL2) 원클릭 설치
# 실행: 관리자 PowerShell에서 `Set-ExecutionPolicy Bypass -Scope Process; .\install.ps1`
# 재실행해도 안전하다 (idempotent).
#
# Neo4j/Docker는 설치하지 않는다. 그래프 기능이 필요하면 Docker Desktop을 별도로 설치하고
# Settings > Resources > WSL Integration > Ubuntu를 켠다.
#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

function Write-Step($n, $msg) {
    Write-Host ""
    Write-Host "[$n] $msg" -ForegroundColor Cyan
}

# ── 1. WSL2 설치 확인 ─────────────────────────────────
Write-Step "1/6" "WSL2 설치 확인"
$wslList = $null
try { $wslList = wsl --list --quiet 2>$null } catch {}
if (-not $wslList -or ($wslList -notmatch "Ubuntu")) {
    Write-Host "  WSL2 및 Ubuntu를 설치한다. 완료 후 재부팅이 필요할 수 있다."
    wsl --install -d Ubuntu
    Write-Host ""
    Write-Host "  WSL 설치가 끝났다. 재부팅 후 이 스크립트를 다시 실행한다." -ForegroundColor Yellow
    Write-Host "  재부팅 후 Ubuntu 터미널이 열리면 사용자 이름/비밀번호를 설정한다."
    Read-Host "  Enter를 눌러 종료"
    exit 0
} else {
    Write-Host "  Ubuntu가 이미 설치되어 있다."
}

# ── 2. Ubuntu 내부 패키지 설치 ─────────────────────────
Write-Step "2/6" "Ubuntu 패키지 설치 (git, python3, tmux, ttyd, pandoc, ffmpeg)"
wsl -d Ubuntu -- bash -c '
set -e
sudo apt-get update -qq
sudo apt-get install -y -qq git python3 python3-pip tmux pandoc ffmpeg 2>/dev/null
# ttyd — apt에 없으면 GitHub 릴리스에서 받는다
if ! command -v ttyd >/dev/null 2>&1; then
  echo "  ttyd 설치 중..."
  ARCH=$(dpkg --print-architecture)
  URL="https://github.com/nicm/ttyd/releases/latest/download/ttyd.${ARCH}"
  # snap이 있으면 snap으로 시도, 아니면 직접 다운로드
  if command -v snap >/dev/null 2>&1; then
    sudo snap install ttyd --classic 2>/dev/null || true
  fi
  if ! command -v ttyd >/dev/null 2>&1; then
    sudo curl -fsSL "https://github.com/nicm/ttyd/releases/latest/download/ttyd.$(uname -m)" -o /usr/local/bin/ttyd 2>/dev/null || true
    sudo chmod +x /usr/local/bin/ttyd 2>/dev/null || true
  fi
fi
echo "  패키지 설치 완료."
'

# ── 3. Node.js LTS 설치 ───────────────────────────────
Write-Step "3/6" "Node.js LTS 설치"
wsl -d Ubuntu -- bash -c '
set -e
if command -v node >/dev/null 2>&1; then
  echo "  Node.js $(node --version) 이미 설치됨."
else
  echo "  Node.js LTS 설치 중..."
  curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash - 2>/dev/null
  sudo apt-get install -y -qq nodejs 2>/dev/null
  echo "  Node.js $(node --version) 설치 완료."
fi
'

# ── 4. Claude Code 설치 ───────────────────────────────
Write-Step "4/6" "Claude Code CLI 설치"
wsl -d Ubuntu -- bash -c '
set -e
if command -v claude >/dev/null 2>&1; then
  echo "  Claude Code 이미 설치됨."
else
  echo "  Claude Code 설치 중..."
  sudo npm install -g @anthropic-ai/claude-code 2>/dev/null
  echo "  Claude Code 설치 완료. 아래 명령으로 로그인한다:"
  echo "    claude"
fi
'

# ── 5. 레포 클론 + setup.sh ───────────────────────────
Write-Step "5/6" "Agent Office 클론 및 초기 설정"
wsl -d Ubuntu -- bash -c '
set -e
TARGET="$HOME/Agent_Office"
if [ -d "$TARGET/.git" ]; then
  echo "  이미 클론되어 있다: $TARGET"
  cd "$TARGET" && git pull --ff-only 2>/dev/null || true
else
  echo "  클론 중..."
  git clone https://github.com/devmoonjs/Agent_Office.git "$TARGET"
fi
cd "$TARGET"
bash setup.sh
echo ""
echo "  볼트 경로: $TARGET"
echo "  분석 대상 repo는 WSL 안에서 클론하고 90-Meta/repos.md에 경로를 적는다."
'

# ── 6. 서버 기동 + 브라우저 ───────────────────────────
Write-Step "6/6" "서버 기동"
Write-Host "  WSL 안에서 서버를 띄운다. 윈도우 브라우저에서 http://127.0.0.1:57910 을 연다."
Write-Host ""

# 백그라운드로 서버를 띄우고 브라우저를 연다
Start-Process wsl -ArgumentList "-d", "Ubuntu", "--", "bash", "-c", "cd ~/Agent_Office && python3 90-Meta/map-ui/server.py"
Start-Sleep -Seconds 2
Start-Process "http://127.0.0.1:57910"

Write-Host ""
Write-Host "설치 완료." -ForegroundColor Green
Write-Host ""
Write-Host "다음 단계:" -ForegroundColor Yellow
Write-Host "  1. Claude Code 로그인: Ubuntu 터미널에서 'claude' 실행"
Write-Host "  2. 분석 대상 repo를 WSL 안에 클론하고 90-Meta/repos.md에 경로 등록"
Write-Host "  3. (선택) Docker Desktop 설치 후 Neo4j 그래프 활성화"
Write-Host "  4. (선택) 설정에서 전사 모델을 medium으로 변경 (더 정확, 더 느림)"
Write-Host ""
Write-Host "주의: WSL은 재부팅 후 자동 기동되지 않는다."
Write-Host "  윈도우 작업 스케줄러에 로그온 시 'wsl -d Ubuntu -- true'를 등록하거나"
Write-Host "  /etc/wsl.conf에 [boot] systemd=true를 설정한다."
