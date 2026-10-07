# lupus

Lupus의 supervisor 코어. 목표·예산·시도·checkpoint·인계 상태와 **기억 그래프**를 로컬 SQLite에 두고, 설치된 `claude`/`codex` CLI를 worker로 실행해 결정적 검사로 완료를 판정한다. Python 3.12+ 표준 라이브러리만 사용한다(SQLite 3.51.3 이상, FTS5 필요). 그래프 보기 화면에만 vis-network를 수정 없이 포함한다([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).

- 설계: [../docs/design/LUPUS-PLAN.md](../docs/design/LUPUS-PLAN.md) · 이번 구현의 근거와 한계: [../docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](../docs/design/LUPUS-IMPLEMENTATION-REVIEW.md)
- 코드가 강제하는 규칙과 범위 밖 항목: [docs/CONTRACT.md](docs/CONTRACT.md)

전역 설정(`~/.claude`, `~/.codex`, PATH)을 바꾸지 않는다. worker와 검증기는 OS 샌드박스 안에서 실행되지만 VM은 아니므로 **보호가 필요한 자료에는 쓰지 않는다.**

## 기본 CLI 대비 측정 (2026-10-06, 이 Mac, 칸당 3회, 모두 통과)

| 한 번에 끝나는 작업 3종 | 평소 설정의 CLI 대비 | 설정을 끈 CLI 대비 |
|---|---|---|
| Claude | 토큰 −91%, 시간 −67% | 토큰 −33%, 시간 −46% |
| Codex | 토큰 −56%, 시간 −51% | 토큰 −47%, 시간 −44% |

`lupus do`는 한 번의 호출로 동작하며 평범한 한 번 호출 대비 Claude에서 토큰 0.89배·시간 1.5배, Codex에서 토큰 0.52배·시간 1.0배다(2026-10-07, 3회씩, holdout은 모두 통과). 실제 프로젝트(tomli)의 upstream 변경 3건은 두 CLI 모두 첫 시도에 끝냈다. 실제 upstream 커밋 20건을 숨긴 테스트로 채점하면 `lupus do`는 평범한 호출과 같은 수준이다(Codex 13 대 13, Claude 12건 중 11 대 10). 더 낫지는 않고 `--review`도 결과를 바꾸지 못했다. 조건·원자료·한계는 [../docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](../docs/design/LUPUS-IMPLEMENTATION-REVIEW.md) 1.13절과 `docs/*.json`. 재현: `PYTHONPATH=src python3 evaluations/compare.py simple out.json 3`, `evaluations/request.py`, `evaluations/real.py`, `evaluations/write.py`.

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

목표 파일 없이 한 줄로(프로젝트 폴더 안에서):

```sh
lupus fix-tests --driver claude                      # 실패하는 테스트를 관측해 목표로 삼는다 (Python·Node·Go·Rust)
lupus fix-tests --driver codex --check "make test" --protect tests   # 알아보지 못하는 프로젝트: 테스트 명령을 직접 지정
lupus fix-tests --driver claude --container node:24  # 테스트를 호스트 샌드박스 대신 Docker 컨테이너에서(네트워크 없음)
lupus do "<요청>" --driver claude [--two-step]       # 한 번 호출로 실패하는 테스트 + 따로 둔 구현안 -> 승인 -> 적용·검증
lupus do "<요청>" --driver claude --review [codex]   # 검사 통과 뒤 구현을 쓰지 않은 AI가 요청과 변경을 대조(이의는 요청 인용 필수, 한 번 돌려보냄)
                                                     # 승인 때 edit 입력: 테스트를 직접 고쳐서 승인. --allow-failing: 지금 실패하는 기존 테스트는 그대로 둠
lupus do "<요청>" --driver claude --isolated          # 커밋된 HEAD의 별도 체크아웃에서 작업 (fix-tests도 가능). --shell: worker가 명령 실행 가능
lupus diff <goal_id> | lupus accept <goal_id> | lupus discard <goal_id>   # 결과 보기 / 커밋 하나로 가져오기(브랜치가 움직였으면 합쳐서 다시 검사한 뒤) / 버리기
lupus do "<요청>" --driver claude --lean              # 구현 방식 지침 추가(재사용 우선·최소 변경. 선택 사항, fix-tests도 가능)
lupus write "<요청>" --out docs/plan.md --driver claude [--judge codex] [--must "<기준>"] [--web]
                                                     # 문서: 기준 승인 -> 작성 -> 다른 AI가 인용하며 평가 -> 판본 승인
lupus session --driver claude [-- <CLI 인자>]        # 평소의 대화형 CLI. 테스트 고정, 종료 시 Lupus가 검증
```

여러 프로젝트와 백그라운드:

```sh
lupus alpha-status                                   # 모든 프로젝트의 열린 목표, 대기 사유, 공유 예산
lupus alpha-budget [--project <id>] --calls 300 --attempts 40 --minutes 600 [--tokens N]
lupus goal-priority <goal_id> 5                      # 큰 수가 먼저
lupus alpha-run --drivers claude,codex [--background] [--parallel N]   # 열린 목표를 차례로(또는 N개 동시에: 서로 다른 프로젝트·격리된 체크아웃). 한도가 떨어지면 다른 AI로 인계
lupus run <goal_id> --driver claude --background     # 터미널을 닫아도 계속
lupus jobs | lupus logs <job_id> | lupus stop <job_id>
lupus learn --driver claude                          # 기록된 실패에서 절차 후보 생성(사건이 없으면 호출하지 않음)
lupus learn --undo [PASS]                            # 학습 묶음이 추가한 후보를 한 번에 철회(모델 호출 없음)
lupus release <goal_id> <파일…>                      # 요청이 고정된 테스트·fixture가 정해 둔 동작을 바꿀 때: 그 파일의 변경을 허용(제안만)
lupus release-approve <goal_id> | lupus release-reject <goal_id> --note "…"   # worker가 제안한 정확한 diff를 보고 승인 / 거절
lupus approve <goal_id> | lupus revise <goal_id> "<의견>" --driver claude   # 문서 목표의 승인·수정
lupus revalidate <goal_id> --note "…"                # 권한 철회 뒤 계속 허용
lupus project-remove <project_id> | lupus prune --days 30
lupus prune --kept                                   # 리뷰가 되돌리기를 끝내지 못해 남겨 둔 프로젝트 사본도 삭제(평소에는 목록만 보여 준다)
lupus --json <명령>                                  # 터미널에서도 전체 JSON 출력(기본은 요약)
```

지식 그래프:

```sh
python3 -m lupus note-add --project <project_id> --kind decision --title "날짜 형식" --body "날짜는 YYYY.MM.DD 로 쓴다"
python3 -m lupus note-search --project <project_id> "보고서 날짜"   # 이 작업에서 worker에게 보일 기록
python3 -m lupus note-link <node_a> supersedes <node_b>           # 대체·관련·상충·부분·파생
python3 -m lupus note-verify|note-retire|note-promote|note-forget <node_id>
python3 -m lupus graph --open                                     # 그래프 화면
```

사용자 권한이 필요한 명령(`project-add`, `goal-submit`, `goal-pause/resume/cancel`, `resolve`, `release`, `release-approve`, `release-reject`, `budget-raise`, `revoke`, `fix-tests`, `do`, `write`, `session`, `learn`, `alpha-budget`, `goal-priority`, `note-add/link/verify/retire/promote/forget`)은 실제 터미널에서 `yes`를 입력해야 실행된다. `run`·`alpha-run`·`jobs`·`stop`은 확인을 묻지 않으므로 백그라운드로 실행할 수 있다.

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

완료 조건은 검증기(`file_contains`, `file_sha256`, `command`, `red_test`, `document`, `judge`, `user_approval`)로 표현한다. `command`에는 `protect`·`forbid_new`·`forbid_new_names`·`frozen_trees`·`must_pass`·`require_tests`·`min_tests`·`env`·`env_pass`·`container`·`sandbox`를 붙일 수 있고, 실행·보호·환경에 관한 것은 user만 정할 수 있다([docs/CONTRACT.md](docs/CONTRACT.md)). 모든 task는 완료 조건 하나 이상에 연결되어야 하며, worker가 완료했다고 답해도 검증기가 통과하지 않으면 완료되지 않는다.

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
| `runners`, `quick` | 테스트 러너 판별과 출력 판독, `fix-tests`·`do` |
| `protect`, `verify` | 검증 수단의 고정·되돌림, 검증기 실행(샌드박스·컨테이너) |
| `service`, `judging`, `author` | 프로젝트 밖에서 실행되는 모델 호출, 문서 평가와 승인, `write` |
| `session`, `hook` | 대화형 세션과 그 안의 확인 |
| `alpha`, `jobs` | 여러 프로젝트의 순서·공유 예산·AI 전환, 백그라운드 실행 |
| `learn` | 기록된 실패에서 절차 후보 만들기, 묶음 단위 되돌리기 |
| `economy` | `--lean` 지침: 동봉한 Ponytail 원문에서 worker에게 맞는 부분만 골라 붙임 |
| `contract` | 초안 테스트가 무엇을 단언하고 worker가 무엇을 스스로 정했는지: 확인 가능한 것은 확인해 승인 화면에 표시 |
| `release` | 고정된 테스트·fixture의 변경: 해제 → 제안 보관 → 정확한 diff 승인 → 새 개정 |
| `gitx` | `.git` 보호, 격리된 체크아웃과 diff·accept·discard |
