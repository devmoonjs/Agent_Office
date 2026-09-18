# 윈도우 설치 (WSL2)

네이티브 윈도우는 지원하지 않는다. WSL2 Ubuntu 안에서 클론해 사용한다.

## 자동 설치

관리자 PowerShell에서 한 줄로 실행한다.

```powershell
Set-ExecutionPolicy Bypass -Scope Process; .\install.ps1
```

스크립트가 WSL2 설치 여부를 확인하고, Ubuntu 패키지·Node.js·Claude Code·레포 클론·서버 기동까지 순서대로 처리한다. 재실행해도 안전하다.

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

### 3단계: Node.js + Claude Code

```bash
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g @anthropic-ai/claude-code
claude    # 브라우저 로그인
```

### 4단계: 클론 및 기동

```bash
git clone https://github.com/devmoonjs/Agent_Office.git
cd Agent_Office
bash setup.sh
python3 90-Meta/map-ui/server.py
```

윈도우 브라우저에서 `http://127.0.0.1:57910` 을 연다.

<!-- 스크린샷: Agent Office 메인 화면 -->

## 반드시 지킬 것

- **분석 대상 repo는 WSL 안에 클론한다.** `/mnt/c/...` 경로의 repo는 git diff가 수 배 느리고 파일 감시가 동작하지 않는다. `90-Meta/repos.md`에는 `/home/<user>/...` 경로만 적는다.
- **스케줄러**: launchd 대신 cron을 사용한다. WSL은 재부팅 후 자동 기동되지 않으므로 윈도우 작업 스케줄러에 로그온 시 `wsl -d Ubuntu -- true`를 등록하거나, `/etc/wsl.conf`의 `[boot] systemd=true`로 cron 서비스를 살린다.
- **회의 전사**: faster-whisper(CPU)로 자동 폴백된다. GPU가 없으면 1시간 녹음에 10분 이상 걸린다. NVIDIA GPU가 WSL에서 잡히면 CUDA가 자동으로 사용된다.
- 윈도우에 Claude Code가 이미 있어도 WSL 안에 별도로 설치하고 로그인해야 한다.

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

그래프 없이도 UI, 스킬, 에이전트 채팅, 회의 전사 등 대부분의 기능은 동작한다.

## 제약 사항

- **실시간 전사**: Apple Silicon의 mlx-whisper에 비해 faster-whisper(CPU)는 느리다. 실시간 회의 보조(답변도우미/참견모드)는 GPU 없이 실용적이지 않을 수 있다.
- **브라우저 열기**: `open` 명령이 없으므로 서버 기동 후 수동으로 브라우저를 연다.
- **파일 감시 경계**: WSL의 inotify는 `/mnt/c` 경로에서 동작하지 않는다. repo를 반드시 WSL 내부에 둔다.

## 문제 해결

### WSL이 설치되지 않는다

윈도우 10 버전 2004 이상이 필요하다. `winver`로 확인한다. 버전이 낮으면 Windows Update를 먼저 실행한다.

### ttyd를 찾을 수 없다

```bash
# snap 경로 확인
which ttyd || snap list ttyd

# 직접 다운로드
sudo curl -fsSL "https://github.com/nicm/ttyd/releases/latest/download/ttyd.$(uname -m)" -o /usr/local/bin/ttyd
sudo chmod +x /usr/local/bin/ttyd
```

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
