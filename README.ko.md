<div align="center">

<img src="./asset/logo/lupuslogo2.png" alt="LUPUS — AI works together" width="380" />

# LUPUS

**AI WORKS TOGETHER**

### Claude Code와 Codex CLI가 검증 가능한 일을 끝까지 마치게 하는 로컬 supervisor —<br/>증거 기반 완료 · 예산 · 중단 복구 · Claude ↔ Codex 인계

[English](./README.md) · [한국어](./README.ko.md) · [简体中文](./README.zh-CN.md) · [日本語](./README.ja.md)

[![License](https://img.shields.io/badge/license-Apache--2.0-1f2937?style=flat-square)](./LICENSE)
![Stage](https://img.shields.io/badge/stage-v0.1%20alpha-d69526?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square)
![Dependencies](https://img.shields.io/badge/runtime%20deps-0-2ea043?style=flat-square)
![Tests](https://img.shields.io/badge/offline%20tests-224%20passing-2ea043?style=flat-square)

</div>

Lupus는 이미 설치해 쓰고 있는 `claude`·`codex` CLI를(기존 구독 로그인 그대로) worker로 실행하고, "완료"는 직접 판정합니다. 모델이 끝났다고 말해서가 아니라 결정적 검사가 통과해야 목표가 끝납니다. 목표·예산·시도·checkpoint·프로젝트 지식을 로컬 SQLite에 두므로 강제 종료, 사용량 한도, AI 교체 뒤에도 일이 이어집니다.

**현재 `v0.1 alpha`입니다.** macOS 1인용 도구이고 격리가 없으며(worker는 사용자 권한으로 실행) 아래 측정은 규모가 작습니다. 민감한 자료에는 쓰지 마세요.

## 하는 일

| | |
|---|---|
| **증거로만 완료** | DONE은 현재 완료 조건에 묶인 검사가 통과해야 합니다. worker의 "고쳤습니다"는 증거가 아닙니다. |
| **테스트 고정** | worker 시작 전에 테스트 파일과 runner 설정을 고정합니다. 수정·삭제·skip된 테스트는 검증 전에 되돌립니다. |
| **Claude ↔ Codex 인계** | 한 AI가 멈추면(한도, 종료, 선택) 다른 AI가 검증된 checkpoint에서 이어갑니다. 끝난 단계는 다시 하지 않고 예산·시도 횟수는 초기화되지 않습니다. |
| **예산과 헛돌기 차단** | 호출·시도·시간·토큰을 시작 전에 예약합니다. 같은 실패의 반복은 모델을 부르기 전에 거부합니다. |
| **중단에 안전한 checkpoint** | 복구 자료를 내구 저장한 뒤 DB에 확정합니다. 모든 경계에서 프로세스를 죽이는 시험으로 확인했습니다. |
| **경량 실행** | 작업에 필요 없는 플러그인·훅·MCP·스킬 설명 없이 시작하고, 작업 파일을 prompt에 넣습니다. |
| **기억 그래프** | 프로젝트 지식을 출처가 있는 노드와 종류가 있는 연결로 저장합니다. 회상은 시도에 묶이고 그 시도의 검증 결과로 평가됩니다. |
| **실패 테스트는 한 줄** | `lupus fix-tests`는 목표 파일이 필요 없습니다. supervisor가 직접 실패를 관측하고 그 실패가 목표가 됩니다. |

## 한 대의 Mac에서 측정 (2026-10-05)

같은 작업, 같은 검사, 실행마다 새 디렉터리. Claude Code 2.1.289, codex-cli 0.160.0. 모든 실행이 검사를 통과했습니다. 토큰 = 새 입력 + 캐시 입력 + 출력.

| 상황 | 평소 설정의 CLI | Lupus |
|---|---|---|
| 한 번에 끝나는 코딩 작업 3종, Claude | 135,729 토큰 · 15.0초 | 12,166 토큰 · 6.2초 |
| 한 번에 끝나는 코딩 작업 3종, Codex | 88,530 토큰 · 26.2초 | 36,298 토큰 · 12.2초 |
| 4단계 프로젝트, Claude | 298,327 토큰 · 52.0초 | 70,768 토큰 · 42.5초 |
| 같은 프로젝트, 중단 후 다른 AI가 인계 | 337,133 토큰 · 82.0초 · 재설명 1,139자 | 138,989 토큰 · 64.1초 · 재설명 없음 |
| 실패 테스트 복구, Claude | 136,712 토큰 · 16.2초 | 12,817 토큰 · 5.7초 · 명령 한 줄 |

정직하게 읽어 주세요.

- **절감의 대부분은 필요 없는 설정을 싣지 않는 것과 prompt 기법(파일 동봉, 한 번에 쓰기)에서 나옵니다.** 같은 기법을 Lupus 없이 쓴 CLI 호출은 한 번에 끝나는 작업에서 같은 토큰을 썼습니다. 그 경우 supervisor가 더하는 것은 싼 토큰이 아니라 검증된 완료입니다.
- 검사를 포함한 목표를 정의하는 데는 평범한 prompt의 약 1.9배 입력이 듭니다(`fix-tests` 제외).
- 인계 시나리오의 Codex 구간은 기본 CLI보다 33% **더** 썼습니다. Lupus는 단계마다 호출하는데 Codex는 호출당 고정 입력이 큽니다.
- 칸당 1~2회 실행입니다. 숨긴 holdout 검사가 있는 더 어려운 작업 3종에서는 36회 모두 통과했고 잘못된 통과가 없어, 테스트 고정이 거짓 완료를 줄인다는 것은 이 벤치마크로 보이지 못했습니다.

원자료와 스크립트: [`lupus/docs/`](./lupus/docs) · [`lupus/evaluations/`](./lupus/evaluations) · 전체 기록 [docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md).

## 쓸 때와 쓰지 않을 때

**쓰는 편이 나은 경우**: 기계로 검사할 수 있는 일. 실패하는 테스트, 테스트가 있는 여러 단계 작업, 중간에 끊기거나 Claude·Codex를 오가야 하는 긴 작업, 한 번 적어 두면 좋은 규칙이 있는 프로젝트.

**기본 CLI가 나은 경우**: 일회성 질문, 대화하며 탐색하는 작업, 플러그인·MCP가 필요한 작업, 프로그램으로 판정할 수 없는 일(기획, 조사, 글, 디자인).

## 빠른 시작

필요 환경: macOS, SQLite가 **3.51.3 이상이고 FTS5가 켜진** Python **3.12+**(Homebrew Python이면 됩니다), 로그인된 `claude` 또는 `codex`.

```bash
git clone https://github.com/djfksjd/lupus.git
cd lupus/lupus
python3 -m pip install -e .          # 또는 export PYTHONPATH=src 후 `python3 -m lupus`

lupus init                           # ~/.lupus 생성 (DB + vault)
lupus probe --live                   # 설치된 CLI가 실제로 지원하는 범위 측정 (작은 호출 2회)

cd ~/work/my-project                 # 실패하는 테스트가 있는 프로젝트
lupus fix-tests --driver claude      # 실패 관측 -> 테스트 고정 -> 수정 -> 검증
lupus do "내보내기 명령에 --json 옵션 추가" --driver claude
                                     # 일반 요청: 실패하는 테스트를 먼저 작성 -> 읽고 승인 -> 구현
```

직접 검사를 정한 목표:

```bash
lupus project-add ~/work/site --name site --providers anthropic,openai
lupus goal-submit <project_id> goal.json
lupus run <goal_id> --driver claude
lupus status <goal_id>               # 상태, 예산, 재개 가능 여부와 차단 사유
lupus run <goal_id> --driver codex   # 다른 AI로 이어가기 (검증된 인계)
lupus graph --open                   # 지식 그래프 화면
```

`goal.json` 형식과 전체 명령: [`lupus/README.md`](./lupus/README.md).

## 동작 방식

```text
 you ──► goal + acceptance checks ──► ┌──────────────── Lupus supervisor (plain program) ───────────────┐
                                      │ budget · attempts · leases · checkpoints · frozen tests · memory │
                                      └───────┬───────────────────────────────────────────────┬─────────┘
                                   lean prompt│                                               │verify (deterministic)
                                              ▼                                               ▼
                                   claude  ◄──handoff──►  codex                    PASS evidence ──► DONE
```

- supervisor는 일반 프로그램입니다. 배정·예산·재시도·완료를 모델 호출로 결정하지 않습니다.
- worker는 prompt와 디렉터리만 받습니다. DB도 CLI도 권한도 받지 않습니다.
- 전역 설정을 건드리지 않습니다. 훅, PATH, `~/.claude`, `~/.codex`를 수정하지 않습니다.

<div align="center">
<img src="./asset/screenshots/graph.png" alt="Lupus 지식 그래프 화면" width="760" />
<br/><sub>지식 그래프 화면(<code>lupus graph</code>): 노드, 종류가 있는 연결, 지식이 기록되고 회상된 목표</sub>
</div>

## 알아야 할 한계

- **VM·컨테이너 격리는 없습니다.** 검증기는 macOS 샌드박스 안에서 실행되고 worker는 각 CLI의 제한 기능으로 가두지만, CLI 자체는 사용자 권한으로 실행됩니다. 보호 자료·고객 자료를 주면 안 됩니다. 읽기 범위는 CLI가 허용하는 만큼 가두고 canary 파일로 실측합니다. Codex는 홈 디렉터리를 읽지 못하고 명령에서 네트워크를 쓰지 못하는 권한 프로필로 실행합니다. 가두기가 확인되지 않은 driver는 프로젝트별 명시적 동의가 필요합니다. [SECURITY.md](./SECURITY.md) 참고.
- 평소 `claude`/`codex` 세션과 연결되지 않습니다. `lupus`를 따로 실행해야 하고 대화형 모드가 없습니다.
- `fix-tests`는 Python `unittest`/`pytest` 프로젝트만 지원합니다.
- 코드가 젊습니다. 외부 리뷰 8회에서 결함 60건을 찾아 고쳤고, 더 남아 있다고 보는 것이 맞습니다.
- 구독 한도에서 실제로 차감된 양은 관측할 수 없습니다. 토큰 수는 CLI가 보고한 값입니다.

## 문서

- [기계 계약](./lupus/docs/CONTRACT.md) — 코드가 강제하는 규칙과 범위 밖 항목
- [구현 기록](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md) — 결정, 리뷰, 모든 측정과 한계
- [설계](./docs/design/LUPUS-PLAN.md) — 구현이 그 일부인 전체 설계
- [서드파티 고지](./lupus/THIRD_PARTY_NOTICES.md) — 그래프 화면에 vis-network를 수정 없이 포함

## 라이선스

[Apache-2.0](./LICENSE). 포함된 vis-network는 MIT로 사용합니다.
