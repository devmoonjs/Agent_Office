# install.ps1 — Agent Office 윈도우(WSL2) 원클릭 설치
#
# 실행 (관리자 PowerShell):
#   irm https://raw.githubusercontent.com/devmoonjs/Agent_Office/main/install.ps1 | iex
#
# 재실행해도 안전하다 (idempotent). 이미 끝난 단계는 건너뛰고 점검만 출력한다.
#
# 이 파일은 UTF-8 BOM + CRLF로 저장해야 한다. BOM이 없으면 Windows PowerShell 5.1이
# 시스템 ANSI 코드페이지(한글 Windows는 CP949)로 읽어 한글 문자열이 깨지고 파싱이 실패한다.
#
# #Requires -RunAsAdministrator 는 iex 실행 시 무시되므로 본문에서 직접 검사한다.
#
# Neo4j/Docker는 설치하지 않는다. 그래프 기능이 필요하면 Docker Desktop을 별도로 설치하고
# Settings > Resources > WSL Integration > Ubuntu를 켠다.

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/devmoonjs/Agent_Office.git"
$Distro  = "Ubuntu"
$Port    = 57910

function Write-Step($n, $msg) {
    Write-Host ""
    Write-Host "[$n] $msg" -ForegroundColor Cyan
}
function Write-OK($msg)   { Write-Host "  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  $msg" -ForegroundColor Red }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# wsl.exe는 출력을 UTF-16LE로 내보낸다. Windows PowerShell 5.1은 이를 ANSI로 디코딩하므로
# 문자열에 NUL이 섞여 "Ubuntu" 매칭이 실패한다. NUL을 걷어낸 뒤 비교한다.
function Get-WslDistros {
    try { return ((wsl --list --quiet 2>$null) -join "`n") -replace "`0", "" }
    catch { return "" }
}

# WSL 안에서 bash 스크립트를 실행한다. -l(로그인 셸)로 PATH를 읽어
# ~/.local/bin 에 설치된 claude 를 찾을 수 있게 한다.
function Invoke-Wsl($script) {
    wsl -d $Distro -- bash -lc $script
    if ($LASTEXITCODE -ne 0) { throw "WSL 명령이 실패했다 (exit $LASTEXITCODE)" }
}

Write-Host ""
Write-Host "Agent Office 설치 (WSL2)" -ForegroundColor Cyan
Write-Host "────────────────────────────────────"

# ── 1/5. WSL2 확인 ────────────────────────────────────
Write-Step "1/5" "WSL2 확인"
if ((Get-WslDistros) -notmatch "Ubuntu") {
    if (-not (Test-Admin)) {
        Write-Fail "WSL 설치에는 관리자 권한이 필요하다."
        Write-Host "  PowerShell을 '관리자 권한으로 실행'한 뒤 같은 명령을 다시 실행한다."
        return
    }
    Write-Warn "Ubuntu가 없다. WSL2와 Ubuntu를 설치한다 (약 1GB, 5~15분)."
    Write-Host "  진행률이 한동안 멈춘 것처럼 보여도 정상이다 — 다운로드 중이다."
    # --web-download: Microsoft Store CDN 대신 직접 내려받는다. Store 경유는
    # 특정 네트워크(사내 프록시·VPN)에서 중간에 멈추는 사례가 많다.
    wsl --install -d Ubuntu --web-download
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "--web-download 가 실패했다. 기본 방식으로 재시도한다."
        wsl --install -d Ubuntu
    }
    Write-Host ""
    Write-Warn "여기까지가 1단계다. 재부팅한 뒤 아래를 순서대로 진행한다."
    Write-Host "    1) 시작 메뉴에서 'Ubuntu'를 열어 사용자명과 비밀번호를 만든다"
    Write-Host "       (비밀번호는 입력해도 화면에 보이지 않는다 — 정상이다)"
    Write-Host "    2) 같은 설치 명령을 다시 실행한다 — 2단계부터 이어진다"
    return
}
Write-OK "Ubuntu 설치됨."

# Ubuntu는 있지만 사용자 계정 생성 전이면 이후 sudo 단계가 모두 막힌다. 먼저 걸러낸다.
$whoami = (wsl -d $Distro -- bash -lc 'id -un 2>/dev/null' | Out-String).Trim()
if (-not $whoami -or $whoami -eq "root") {
    Write-Warn "Ubuntu 사용자 계정이 아직 없다."
    Write-Host "  시작 메뉴에서 'Ubuntu'를 열어 사용자명과 비밀번호를 만든 뒤 다시 실행한다."
    return
}
Write-OK "사용자: $whoami"

# ── 2/5. Ubuntu 패키지 ────────────────────────────────
Write-Step "2/5" "패키지 설치 (git, python3, tmux, ttyd, pandoc, ffmpeg)"
Write-Host "  sudo 비밀번호를 물으면 Ubuntu 계정 비밀번호를 입력한다."
Write-Host "  pandoc과 ffmpeg가 커서 5~15분 걸릴 수 있다."
Invoke-Wsl @'
set -e
sudo apt-get update -qq
sudo apt-get install -y -qq git python3 python3-pip tmux pandoc ffmpeg curl

# ttyd — 터미널 탭용. apt/snap에 없으므로 공식 릴리스의 정적 바이너리를 쓴다.
# (구버전 스크립트는 존재하지 않는 nicm/ttyd 를 받으려다 조용히 실패했다.)
if ! command -v ttyd >/dev/null 2>&1; then
  echo "  ttyd 설치 중..."
  if sudo curl -fsSL "https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.$(uname -m)" \
       -o /usr/local/bin/ttyd; then
    sudo chmod +x /usr/local/bin/ttyd
  else
    echo "  경고: ttyd 다운로드 실패 — 터미널 탭이 비활성화된다."
  fi
fi
echo "  패키지 설치 완료."
'@

# ── 3/5. Claude Code ──────────────────────────────────
# npm 전역 설치는 쓰지 않는다. npm 11.19+ 가 install script를 기본 차단해
# claude-code의 postinstall(node install.cjs)이 건너뛰어지고 반쪽 설치가 된다.
# 공식 설치 스크립트는 ~/.local/bin 에 넣으므로 Node.js 자체가 불필요하다.
Write-Step "3/5" "Claude Code 설치"
Invoke-Wsl @'
set -e
if command -v claude >/dev/null 2>&1; then
  echo "  이미 설치됨: $(claude --version 2>/dev/null || echo unknown)"
else
  echo "  설치 중..."
  # sudo 를 붙이면 설치 스크립트가 거부한다 ($HOME 이 root 로 잡히기 때문).
  curl -fsSL https://claude.ai/install.sh | bash
  grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null || \
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
  echo "  설치 완료."
fi
'@

# ── 4/5. 클론 + 초기 설정 ─────────────────────────────
Write-Step "4/5" "Agent Office 내려받기"
Invoke-Wsl @"
set -e
TARGET="`$HOME/Agent_Office"
if [ -d "`$TARGET/.git" ]; then
  echo "  이미 있다: `$TARGET"
  cd "`$TARGET" && git pull --ff-only 2>/dev/null || echo "  (로컬 변경이 있어 pull 생략 — 설정 화면의 업데이트 버튼을 쓴다)"
else
  git clone --quiet $RepoUrl "`$TARGET"
  echo "  내려받기 완료: `$TARGET"
fi
cd "`$TARGET"
bash setup.sh
"@

# ── 5/5. 점검 + 기동 ──────────────────────────────────
Write-Step "5/5" "설치 점검"
wsl -d $Distro -- bash -lc @'
ok()   { printf "  %-10s %s\n" "$1" "$2"; }
fail() { printf "  %-10s [누락] %s\n" "$1" "$2"; }
v() { command -v "$1" >/dev/null 2>&1 && ok "$1" "$($2 2>/dev/null | head -1)" || fail "$1" "$3"; }

v git    "git --version"     "sudo apt install -y git"
v python3 "python3 --version" "sudo apt install -y python3"
v tmux   "tmux -V"           "sudo apt install -y tmux"
v pandoc "pandoc --version"  "sudo apt install -y pandoc"
v ffmpeg "ffmpeg -version"   "sudo apt install -y ffmpeg"
v ttyd   "ttyd --version"    "터미널 탭 비활성 — 재실행하면 다시 시도한다"
v claude "claude --version"  "curl -fsSL https://claude.ai/install.sh | bash"
[ -d "$HOME/Agent_Office" ] && ok "볼트" "$HOME/Agent_Office" || fail "볼트" "클론 실패"
'@

Write-Step "기동" "서버 시작"
Start-Process wsl -ArgumentList "-d", $Distro, "--", "bash", "-lc",
    "cd ~/Agent_Office && python3 90-Meta/map-ui/server.py; echo; read -p '창을 닫으려면 Enter'"
Start-Sleep -Seconds 3
Start-Process "http://127.0.0.1:$Port"

Write-Host ""
Write-Host "설치 완료." -ForegroundColor Green
Write-Host ""
Write-Host "다음 단계:" -ForegroundColor Yellow
Write-Host "  1. 브라우저에서 설정(왼쪽 아래 톱니) > Claude 연결 을 눌러 로그인한다"
Write-Host "  2. 같은 화면의 '관리 프로젝트'에서 분석할 폴더를 추가한다"
Write-Host "  3. (선택) Docker Desktop 설치 후 지식 그래프를 켠다"
Write-Host ""
Write-Host "주소: http://127.0.0.1:$Port"
Write-Host "업데이트: 설정 화면의 [업데이트] 버튼"
Write-Host ""
Write-Host "참고: WSL은 재부팅 후 자동 기동되지 않는다. 서버를 다시 띄우려면" -ForegroundColor DarkGray
Write-Host "      Ubuntu 터미널에서  cd ~/Agent_Office && python3 90-Meta/map-ui/server.py" -ForegroundColor DarkGray
