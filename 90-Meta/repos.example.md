# 분석 대상 폴더 목록

한 줄에 절대경로 하나. `#` 이후는 주석. 이 파일은 사용자 전용이며 에이전트는 읽기만 한다.

`setup.sh`가 이 파일을 `repos.md`로 복사한다. `repos.md`는 git 추적 대상이 아니므로
업데이트해도 내용이 보존된다.

```
# /home/me/work/my-service       # 예시 — 자신의 경로로 바꾼다
# /home/me/문서/장비심의          # git 저장소가 아닌 일반 폴더도 된다
```

## 참고

- 설정 화면(⚙) > **관리 프로젝트**에서 추가한 폴더는 이 파일과 별개로 관리된다
  (`.agent-office/projects.json`). 둘 다 합쳐서 프로젝트 목록이 된다.
- **WSL을 쓴다면 폴더를 WSL 안(`/home/...`)에 둔다.** `/mnt/c/...` 경로는 git diff가
  수 배 느리고 파일 감시가 불안정하다.
- 경로 뒤에 `|`로 옵션을 붙일 수 있다.

```
/home/me/work/big-repo | branch=develop, max-diff=300
```
