# lupus

Lupus의 supervisor 코어. 목표·예산·시도·checkpoint·인계 상태와 **기억 그래프**를 로컬 SQLite에 두고, 설치된 `claude`/`codex` CLI를 worker로 실행해 결정적 검사로 완료를 판정한다. Python 3.12+ 표준 라이브러리만 사용한다(SQLite 3.51.3 이상, FTS5 필요). 그래프 보기 화면에만 vis-network를 수정 없이 포함한다([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).

- 설계: [../docs/design/LUPUS-PLAN.md](../docs/design/LUPUS-PLAN.md) · 이번 구현의 근거와 한계: [../docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](../docs/design/LUPUS-IMPLEMENTATION-REVIEW.md)
- 코드가 강제하는 규칙과 범위 밖 항목: [docs/CONTRACT.md](docs/CONTRACT.md)

전역 설정(`~/.claude`, `~/.codex`, PATH, 훅)을 바꾸지 않는다. worker는 사용자와 같은 권한으로 실행되므로 **보호가 필요한 자료에는 쓰지 않는다.**

## 기본 CLI 대비 측정 (2026-10-05, 이 Mac, 소수 반복)

| 상황 | 평소 설정의 CLI 대비 | 설정을 끈 CLI 대비 |
|---|---|---|
| 한 번에 끝나는 작업 3종, Claude | 토큰 −91%, 시간 −59% | 토큰 −32%, 시간 −43% |
| 한 번에 끝나는 작업 3종, Codex | 토큰 −59%, 시간 −53% | 토큰 −48%, 시간 −48% |
| 4단계 프로젝트, Claude | 토큰 −76%, 시간 −18% | 측정 안 함 |

모든 실행이 같은 검증기를 통과했다. 조건·원자료·한계는 [../docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](../docs/design/LUPUS-IMPLEMENTATION-REVIEW.md) 1.8절과 `docs/compare-*.json`. 재현: `PYTHONPATH=src python3 evaluations/compare.py simple out.json`.

## 시험

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -t .              # 오프라인
LUPUS_LIVE=1 PYTHONPATH=src python3 -m unittest tests.test_native_live  # 실제 CLI, 구독 사용량 소량 소모
```

## 사용

```sh
export PYTHONPATH=src LUPUS_HOME=~/.lupus     # 또는 pip install -e .
python3 -m lupus init
python3 -m lupus probe --live                 # 이 Mac에서 CLI가 실제로 지원하는 범위 측정
python3 -m lupus project-add ~/work/site --name site --providers anthropic,openai
python3 -m lupus goal-submit <project_id> goal.json
python3 -m lupus run <goal_id> --driver claude   # --cheap-first: 가벼운 모델 먼저, 검증 실패 시 기본 모델
python3 -m lupus status <goal_id>             # 상태, 예산, 재개 가능 여부와 차단 사유
python3 -m lupus run <goal_id> --driver codex # 다른 AI로 이어가기 (검증된 handoff)
python3 -m lupus vault-sync                   # 전용 Vault(<home>/vault) 갱신. run 뒤에는 자동
```

지식 그래프:

```sh
python3 -m lupus note-add --project <project_id> --kind decision --title "날짜 형식" --body "날짜는 YYYY.MM.DD 로 쓴다"
python3 -m lupus note-search --project <project_id> "보고서 날짜"   # 이 작업에서 worker에게 보일 기록
python3 -m lupus note-link <node_a> supersedes <node_b>           # 대체·관련·상충·부분·파생
python3 -m lupus note-verify|note-retire|note-promote|note-forget <node_id>
python3 -m lupus graph --open                                     # 그래프 화면
```

사용자 권한이 필요한 명령(`project-add`, `goal-submit`, `goal-pause/resume/cancel`, `resolve`, `budget-raise`, `revoke`, `note-add/link/verify/retire/promote/forget`)은 실제 터미널에서 `yes`를 입력해야 실행된다.

`goal.json`:

```json
{
  "objective": "문의 페이지에 연락처 안내 추가",
  "budget": {"calls": 40, "attempts": 6, "active_ms": 1800000},
  "criteria": [
    {"id": "c0", "text": "contact.html에 이메일 주소가 있다",
     "verifier": {"kind": "file_contains", "path": "contact.html", "text": "help@example.com"}},
    {"id": "c1", "text": "테스트가 통과한다",
     "verifier": {"kind": "command", "argv": ["npm", "test"], "paths": ["contact.html"]}}
  ],
  "tasks": [
    {"title": "안내 추가", "prompt": "contact.html에 help@example.com 안내를 추가하라.", "criteria": ["c0"]},
    {"title": "테스트", "prompt": "테스트가 통과하도록 고쳐라.", "criteria": ["c1"], "depends_on": [0]}
  ]
}
```

완료 조건은 검증기(`file_contains`, `file_sha256`, `command`)로 표현한다. 모든 task는 완료 조건 하나 이상에 연결되어야 하며, worker가 완료했다고 답해도 검증기가 통과하지 않으면 완료되지 않는다.

## 구조

| 모듈 | 책임 |
|---|---|
| `kernel` | DB 연결, 원자적 명령, idempotency, 감사 로그 |
| `projects`, `goals` | 프로젝트 바인딩, 목표·완료 조건·task·증거, 상태 계산 |
| `budget`, `usage` | 예약·정산, 사용량 원장 |
| `runs` | writer 자리·lease·fencing, 시도와 무진전 통제 |
| `recovery` | 복구 객체, checkpoint commit, 재시작 대조, 재개 가능 판정 |
| `actions` | 승인, 외부 동작 intent와 대조 |
| `handoff` | AI 간 인계 packet |
| `adapters`, `gate` | claude/codex/fake worker 실행, 프로세스 그룹 관리 |
| `supervisor` | foreground 실행 루프와 crash 후 정리 |
| `memory` | 기억 그래프: 노드·연결·범위, 회상, 결과 피드백 |
| `vault`, `graph` | 그래프의 Markdown 투영(전용 Vault)과 그래프 보기 화면 |
| `migrations/` | 스키마(순서 있는 SQL 파일) |
| `probe` | CLI 지원 범위 측정 |
