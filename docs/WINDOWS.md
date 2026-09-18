# 윈도우 설치 (WSL2)

네이티브 윈도우는 지원하지 않는다. WSL2 Ubuntu 안에서 클론해 사용한다.

## 자동 설치

관리자 PowerShell에서 한 줄로 실행한다.

```powershell
irm https://raw.githubusercontent.com/devmoonjs/Agent_Office/main/install.ps1 | iex
```

WSL2 확인 → Ubuntu 패키지 → Claude Code → 클론 → 설치 점검 → 서버 기동 순으로 처리한다. 재실행해도 안전하며, 이미 끝난 단계는 건너뛴다.

WSL2가 없으면 1단계에서 설치만 하고 멈춘다. 재부팅한 뒤:

1. 시작 메뉴에서 **Ubuntu**를 열어 사용자명과 비밀번호를 만든다 (비밀번호는 입력해도 화면에 보이지 않는다)
2. 같은 명령을 다시 실행한다 — 2단계부터 이어진다

### 파일로 받아 실행하려면

`.ps1`은 **UTF-8 BOM**으로 저장돼야 한다. BOM이 없으면 Windows PowerShell 5.1이 시스템 ANSI(한글 Windows는 CP949)로 읽어 한글 문자열이 깨지고 파싱이 실패한다. 이 저장소의 `install.ps1`은 BOM과 CRLF로 관리되지만, 브라우저나 편집기를 거치면 유실될 수 있다.

```powershell
$u = 'https://raw.githubusercontent.com/devmoonjs/Agent_Office/main/install.ps1'
$p = "$env:USERPROFILE\Downloads\install.ps1"
[IO.File]::WriteAllText($p, (irm $u), [Text.UTF8Encoding]::new($true))
Set-ExecutionPolicy Bypass -Scope Process -Force
& $p
```

`irm | iex` 방식은 HTTP 응답의 charset으로 디코딩하므로 이 문제가 없다.

## 수동 설치

자동 설치가 막히거나 세부 제어가 필요하면 아래 절차를 따른다.

### 1단계: WSL2 준비 (윈도우, 1회)

1. PowerShell(관리자)에서 실행한다.

```powershell
wsl --install
```

2. 재부팅 후 Ubuntu 터미널이 열리면 사용자 이름과 비밀번호를 설정한다.

<!-- 스크린샷: WSL 설치 완료 화면 -->

### 2단계: 패키지 설치 (Ubuntu 터미널)

```bash
sudo apt update && sudo apt install -y git python3 python3-pip tmux ttyd pandoc ffmpeg
```

`ttyd`가 apt에 없으면 snap으로 설치한다.

```bash
sudo snap install ttyd --classic
```

### 3단계: Claude Code

Node.js는 필요 없다. 공식 설치 스크립트가 `~/.local/bin`에 바이너리를 넣는다.

```bash
curl -fsSL https://claude.ai/install.sh | bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
claude    # 브라우저 로그인
```

`sudo`를 붙이면 안 된다. 설치 스크립트가 거부한다 — `$HOME`이 root로 잡혀 바이너리가 `/root/.local/bin`에 들어가기 때문이다.

npm 전역 설치(`sudo npm install -g @anthropic-ai/claude-code`)는 권장하지 않는다. npm 11.19부터 install script를 기본 차단해 `postinstall: node install.cjs`가 실행되지 않고 반쪽 설치가 된다. 굳이 npm을 쓴다면 `--allow-scripts=@anthropic-ai/claude-code`를 붙인다.

### 4단계: 클론 및 기동

```bash
git clone https://github.com/devmoonjs/Agent_Office.git ~/Agent_Office
cd ~/Agent_Office
bash setup.sh
python3 90-Meta/map-ui/server.py
```

윈도우 브라우저에서 `http://127.0.0.1:57910` 을 연다.

<!-- 스크린샷: Agent Office 메인 화면 -->

## 업데이트

새 버전이 나오면 설정(⚙) 버튼에 점이 찍힌다. 설정 화면 > **버전** 절의 `[업데이트]`를 누르면 5단계 진행률 게이지와 로그를 보여주면서 진행한다 — 원격 확인, 로컬 수정 백업, 내려받기, 설정·의존성 점검, 재시작 준비. 끝나면 서버가 자동으로 재시작되고 화면이 새로고침된다.

재시작 구간에는 브라우저 연결이 잠시 끊긴다. 화면이 멈춘 것처럼 보여도 정상이며, 서버가 다시 응답하면 자동으로 넘어간다.

터미널에서:

```bash
cd ~/Agent_Office
bash update.sh --check    # 상태만 조회
bash update.sh            # 업데이트
```

사용자 파일(`90-Meta/repos.md`, `90-Meta/.env`, `.agent-office/`, 볼트 노트)은 git 추적 대상이 아니므로 업데이트가 덮어쓰지 않는다. 추적 파일을 직접 고쳤다면 업데이트가 중단되고, `[강제 업데이트]`를 쓰면 `.agent-office/backup/<날짜>/`에 백업한 뒤 덮어쓴다.

## 반드시 지킬 것

- **분석 대상 repo는 WSL 안에 클론한다.** `/mnt/c/...` 경로의 repo는 git diff가 수 배 느리고 파일 감시가 동작하지 않는다. `90-Meta/repos.md`에는 `/home/<user>/...` 경로만 적는다.
- **스케줄러**: launchd 대신 cron을 사용한다. WSL은 재부팅 후 자동 기동되지 않으므로 윈도우 작업 스케줄러에 로그온 시 `wsl -d Ubuntu -- true`를 등록하거나, `/etc/wsl.conf`의 `[boot] systemd=true`로 cron 서비스를 살린다.
- **회의 전사**: faster-whisper(CPU)로 자동 폴백된다. GPU가 없으면 1시간 녹음에 10분 이상 걸린다. NVIDIA GPU가 WSL에서 잡히면 CUDA가 자동으로 사용된다.
- 윈도우에 Claude Code가 이미 있어도 WSL 안에 별도로 설치하고 로그인해야 한다.

### 회의 저장이 실패한다

> 저장 실패 — review-ui(57900)에 연결할 수 없습니다

구버전에서 녹음 저장을 Docker 컨테이너에 프록시하던 때의 오류다. 최신 버전은 녹음을 서버가 직접 저장하므로 Docker 없이 동작한다. 설정 화면의 업데이트로 해결된다.

녹음 저장에 필요한 것은 디스크 쓰기뿐이다. 전사에는 `ffmpeg`가, 회의록 작성에는 Claude 로그인이 필요하다.

```bash
ls ~/Agent_Office/00-Inbox/recordings/   # 저장된 녹음 확인
```

## 그래프 기능 (선택)

Neo4j 그래프를 사용하려면 Docker Desktop이 필요하다.

1. Docker Desktop을 설치한다.
2. Settings > Resources > WSL Integration > Ubuntu를 켠다.
3. Agent Office 설정(톱니바퀴)에서 "지식 그래프 사용"을 체크한다.
4. Ubuntu 터미널에서 그래프 컨테이너를 띄운다.

```bash
cd Agent_Office/90-Meta/neo4j
docker compose up -d
```

그래프 없이도 UI, 스킬, 에이전트 채팅, **회의 녹음·전사·회의록 작성**은 모두 동작한다. Docker가 필요한 것은 인물 관계 그래프와 관계 검토 화면뿐이다.

회의 폼의 참석자 목록은 그래프에서 가져오므로 Docker가 없으면 비어 있다. 이름을 직접 입력하면 된다.

## 제약 사항

- **실시간 전사**: Apple Silicon의 mlx-whisper에 비해 faster-whisper(CPU)는 느리다. 실시간 회의 보조(답변도우미/참견모드)는 GPU 없이 실용적이지 않을 수 있다.
- **브라우저 열기**: `open` 명령이 없으므로 서버 기동 후 수동으로 브라우저를 연다.
- **파일 감시 경계**: WSL의 inotify는 `/mnt/c` 경로에서 동작하지 않는다. repo를 반드시 WSL 내부에 둔다.

## 문제 해결

### WSL이 설치되지 않는다

윈도우 10 버전 2004 이상이 필요하다. `winver`로 확인한다. 버전이 낮으면 Windows Update를 먼저 실행한다.

### ttyd를 찾을 수 없다 / 터미널 탭이 안 뜬다

WSL에서 snap은 기본적으로 동작하지 않는다. 공식 릴리스의 정적 바이너리를 받는다. 저장소는 `tsl0922/ttyd`다.

```bash
sudo curl -fsSL "https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.$(uname -m)" -o /usr/local/bin/ttyd
sudo chmod +x /usr/local/bin/ttyd
ttyd --version
```

설치돼 있는데도 터미널이 빈 화면이면 바인딩 주소를 확인한다.

```bash
ss -tlnp | grep ttyd
```

`127.0.0.1:579xx`가 정상이다. `10.255.255.254`로 나오면 구버전이다 — WSL2에는 `lo`(127.0.0.1) 외에 `loopback0`(10.255.255.254)이 있어서 ttyd가 인터페이스 이름 `lo`를 `loopback0`에 잘못 매칭한 것이다. 설정 화면의 업데이트로 해결된다.

버튼을 눌러도 아무 반응이 없으면 브라우저의 팝업 차단을 확인한다.

### 서버가 뜨지만 브라우저에서 접속이 안 된다

WSL의 127.0.0.1은 윈도우 호스트와 공유된다. 방화벽이 차단하는 경우가 있다. `localhost:57910`으로도 시도한다.

### 전사가 매우 느리다

설정에서 전사 모델을 `small`(기본)로 확인한다. NVIDIA GPU가 있으면 WSL에 CUDA 드라이버가 잡혀 있는지 확인한다.

```bash
nvidia-smi    # GPU가 보여야 한다
```

### 재부팅 후 서버가 안 뜬다

WSL은 재부팅 시 자동 기동되지 않는다. 두 가지 방법이 있다.

1. 윈도우 작업 스케줄러에 로그온 시 실행 등록:

```
프로그램: wsl
인수: -d Ubuntu -- bash -c "cd ~/Agent_Office && nohup python3 90-Meta/map-ui/server.py &"
```

2. `/etc/wsl.conf`에 systemd 활성화 후 systemd 서비스 등록:

```ini
[boot]
systemd=true
```

## 미검증 항목

이 문서는 macOS 환경에서 작성되었다. 실제 윈도우 기기에서의 검증은 완료되지 않았다. 특히 다음 항목은 실기기 확인이 필요하다.

- WSL2의 localhost 포워딩 동작 (ttyd iframe 연결)
- CUDA GPU 자동 감지 (faster-whisper)
- Docker Desktop WSL Integration을 통한 Neo4j 접근
- snap을 통한 ttyd 설치 경로
