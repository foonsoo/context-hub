# Context Hub

AI 코딩 클라이언트(Claude Code, Codex, Cursor 등)가 달라져도, 세션이 바뀌어도 작업 맥락이 이어지게 합니다.
작업 에이전트는 도구 3개(`recall`, `note`, `handoff`)만 알면 되고, 대화 기록은 자동으로 모아 백그라운드 큐레이터가 지식으로 정리합니다.

- **원본은 평범한 파일**: `~/context-hub` 아래 Markdown + git. 사람이 읽고 고칠 수 있고 Obsidian으로 열면 그래프로 보입니다.
- **클라이언트 비종속**: MCP 서버와 CLI(`ctx`)를 같은 기능으로 제공합니다. 클라이언트별 차이는 수집 어댑터와 규칙 파일에만 있습니다.
- **AI가 만든 지식은 제안일 뿐**: 새 지식은 `proposed`로 저장되고, 사람이 `ctx review`로 승인해야 `active`가 됩니다. 모든 지식은 증거(수집함의 한 줄)를 가리킵니다.

## 설치 (처음 한 번)

```bash
pip install context-hub          # 또는: pipx install context-hub / uv tool install context-hub
ctx init                         # ~/context-hub 생성, 등록 계획만 보여 줌
ctx init --register              # Claude Code·Codex·Cursor에 MCP·규칙·훅 등록
```

`--register`는 다음을 바꿉니다(바꾸기 전 파일은 `*.context-hub.bak`으로 백업).

| 클라이언트 | 변경 |
| --- | --- |
| Claude Code | `claude mcp add --scope user context-hub`, `~/.claude/CLAUDE.md`에 3줄 규칙, `~/.claude/settings.json`에 훅 4개 |
| Codex | `codex mcp add context-hub`, `~/.codex/AGENTS.md`에 3줄 규칙 |
| Cursor | `~/.cursor/mcp.json`에 서버 추가 |

되돌리기: `ctx uninstall` (허브와 지식은 남습니다). 클라이언트는 다시 시작해야 반영됩니다.

기존 context-memory를 쓰던 경우: `ctx import-legacy`로 미리보기, `ctx import-legacy --apply`로 가져옵니다.
같은 저장소의 여러 사본은 하나의 프로젝트로 합쳐지고, 홈 디렉터리나 `/`에 묶여 있던 기억은 전역 지식으로 갑니다.

## 평소: 그냥 일합니다

1. 아무 클라이언트에서 "어제 하던 거 이어서 해줘"라고 말합니다.
2. 에이전트가 `recall`로 현재 프로젝트의 NOW.md와 관련 지식을 받습니다. Claude Code는 훅이 세션 시작 때 자동으로 넣어 줍니다.
3. 결정이 나오면 에이전트가 `note`로 남깁니다. "이거 기억해"라고 말해도 됩니다.
4. 끝날 때 에이전트가 `handoff`로 한 일과 다음 할 일을 남깁니다.
5. 에이전트가 2~4를 잊어도, 대화 기록을 수집해 큐레이터가 정리합니다(LLM 설정 시).

다른 프로젝트도 이름으로 부를 수 있습니다. A 폴더에서 "B 프로젝트 진행 상태와 할 일 알려줘"라고 하면 B의 상태가 옵니다.
별칭은 `ctx projects alias <slug> "B 프로젝트" b`로 붙입니다.

## 하루 한 번: 검토 (약 2분)

```bash
ctx review          # y=승인 n=기각 e=편집 후 승인 s=건너뛰기
```

## 명령 요약

| 하고 싶은 것 | 명령 |
| --- | --- |
| 검색 | `ctx recall "왜 Postgres로 바꿨지"` |
| 현재 상태 | `ctx now`, `ctx now <프로젝트>`, `ctx now --all` |
| 기록 | `ctx note "결정: ..."`, `ctx handoff --done "..." --next "a;b" --blocker "..."` |
| 프로젝트 | `ctx projects`, `ctx projects alias <slug> <별칭...>`, `ctx projects capture <slug> off` |
| 지식 보기·보관 | `ctx show <id> --sources`, `ctx forget <id>` |
| 대화 기록 정리 | `ctx curate` (수집 + 지식화), `ctx daemon install` (macOS 15분 주기) |
| 상태 | `ctx status` |
| 다른 기기와 공유 | `ctx sync add <비공개 git 주소>`, 이후 `ctx sync` |

## 큐레이터 (선택)

대화 기록을 지식으로 바꾸려면 LLM이 필요합니다.

```bash
pip install 'context-hub[llm]'
export ANTHROPIC_API_KEY=...       # 또는 `ant auth login`
ctx curate --now
```

- 기본 모델은 `claude-opus-5-5`, effort `medium`. `CTX_CURATOR_MODEL`, `CTX_CURATOR_EFFORT`로 바꿉니다.
- 하루 호출 한도 `CTX_CURATOR_DAILY_CALLS`(기본 40). `CTX_CURATOR=off`로 끕니다.
- 세션은 10분 동안 조용해진 뒤 처리합니다. 근거 줄 범위가 맞지 않는 제안은 버립니다.
- 기존 `active` 지식은 절대 직접 바꾸지 않고 검토 대기열로 보냅니다. 사람이 고친 페이지도 마찬가지입니다.

## 프로젝트를 정하는 규칙

읽기(`recall`): 명시한 `project` 인자 → 질문에 나온 프로젝트 이름·별칭 → 현재 폴더.
쓰기(`note`, `handoff`): 명시한 인자 → 현재 폴더 → (폴더가 프로젝트가 아닐 때만) 글에 나온 이름. 다른 프로젝트를 언급만 한 메모가 엉뚱한 곳에 저장되지 않게 하려는 것입니다.

현재 폴더는 `projects.toml`의 경로(가장 긴 일치) → git 원격 주소 → git 루트 순으로 찾습니다.
하위 폴더, worktree, 다른 위치의 clone, 대소문자만 다른 경로는 같은 프로젝트입니다.
홈 디렉터리와 `/`는 프로젝트가 되지 않고, git 저장소가 아닌 폴더는 대화 기록만으로는 자동 등록되지 않습니다.

## 허브 구조

```
~/context-hub/
├── inbox/                 # append-only 증거 (대화 기록, note, handoff)
├── knowledge/
│   ├── global/            # 프로젝트와 무관한 선호·절차
│   └── projects/<slug>/   # NOW.md + decisions/ facts/ tasks/ ...
├── review/                # 검토 대기 항목
├── projects.toml          # 프로젝트 이름·별칭·경로·원격
└── .index/ .state/        # 파생 데이터 (git 제외, 지워도 재생성)
```

## 보안

- 수집 단계에서 API 키, 토큰, 개인 키, 비밀번호 대입, URL 속 자격 증명을 `[REDACTED]`로 바꿉니다. 완벽하지 않으니 비밀을 대화에 붙이지 마세요.
- 허브는 평문입니다. `ctx sync`에는 반드시 비공개 저장소를 쓰세요.
- 수집을 원하지 않는 프로젝트는 `ctx projects capture <slug> off`.

## 개발

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest            # 전체 (wheel 설치 테스트 포함)
.venv/bin/pytest -m "not slow"
```
