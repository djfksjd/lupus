<div align="center">

<img src="./asset/logo/lupuslogo2.png" alt="LUPUS — AI works together" width="380" />

# LUPUS

**AI WORKS TOGETHER**

### Claude Code와 Codex CLI가 검증 가능한 일을 끝까지 마치게 하는 로컬 supervisor —<br/>증거 기반 완료 · 예산 · 중단 복구 · Claude ↔ Codex 인계

[English](./README.md) · [한국어](./README.ko.md) · [简体中文](./README.zh-CN.md) · [日本語](./README.ja.md)

[![License](https://img.shields.io/badge/license-Apache--2.0-1f2937?style=flat-square)](./LICENSE)
![Stage](https://img.shields.io/badge/stage-v0.2%20alpha-d69526?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square)
![Dependencies](https://img.shields.io/badge/runtime%20deps-0-2ea043?style=flat-square)
![Tests](https://img.shields.io/badge/offline%20tests-275%20passing-2ea043?style=flat-square)

</div>

Lupus는 이미 설치해 쓰고 있는 `claude`·`codex` CLI를(기존 구독 로그인 그대로) worker로 실행하고, "완료"는 스스로 판정합니다. worker가 손댈 수 없는 검사가 통과해야 완료이며, 모델이 끝났다고 말했다는 이유로는 완료되지 않습니다.

**현재 `v0.2 alpha`입니다.** macOS 1인용 도구입니다. worker와 검증기는 OS 샌드박스 안에서 실행되지만 VM은 아닙니다. 아래 측정은 규모가 작습니다. 민감한 자료에는 쓰지 마세요.

## 하는 일

| | |
|---|---|
| **증거로만 완료** | 현재 완료 조건에 묶인 검사가 통과해야 DONE입니다. worker의 "고쳤습니다"는 증거가 아닙니다. |
| **테스트 고정** | worker가 시작하기 전에 테스트 파일과 러너 설정을 고정합니다. 수정·삭제·건너뛰기는 검증 전에 되돌립니다. |
| **Python, Node, Go, Rust, 그리고 임의의 명령** | unittest, pytest, node:test, jest, vitest, `go test`, `cargo test`를 프로젝트 파일만 보고 알아봅니다. 그 밖에는 `--check "<테스트 명령>"`. |
| **어떤 요청이든 한 줄** | `lupus fix-tests`는 직접 관측한 실패를 목표로 삼습니다. `lupus do "<요청>"`은 실패하는 테스트를 먼저 쓰고, 승인을 받은 뒤 구현합니다. |
| **문서·기획·조사** | `lupus write`: 먼저 판정 기준을 승인받고, 작성자와 다른 AI가 평가하되 충족이라고 볼 때마다 문서를 인용해야 하며, 마지막에 그 판본을 사용자가 승인합니다. |
| **평소의 대화형 세션** | `lupus session`은 평소 쓰던 `claude`/`codex` 화면을 사용자 설정 그대로 띄우고, 테스트를 고정한 뒤, 종료하면 직접 검증합니다. |
| **Claude ↔ Codex 인계** | 한쪽이 멈추면(한도, 중단, 선택) 다른 쪽이 검증된 checkpoint에서 이어갑니다. 끝난 단계는 다시 하지 않고 예산과 시도 횟수도 초기화되지 않습니다. |
| **여러 프로젝트, 백그라운드** | `lupus alpha-run`은 열려 있는 모든 목표를 공유 예산 아래 차례로 진행하고, 한 AI의 한도가 떨어지면 다른 AI로 넘깁니다. `--background`는 터미널을 닫아도 계속 실행합니다. |
| **예산과 반복 통제** | 호출·시도·시간·토큰을 시작 전에 예약합니다. 같은 실패의 반복은 모델을 부르기 전에 거부합니다. |
| **중단에 안전한 checkpoint** | 복구 자료를 DB commit보다 먼저 내구 저장합니다. 모든 경계에서 프로세스를 죽여 시험했습니다. |
| **OS 수준의 가두기** | Claude worker와 모든 검증기는 Lupus가 적용한 macOS 샌드박스 안에서 실행됩니다. 검증기는 Docker 컨테이너에서 돌릴 수도 있습니다. |
| **자리를 증명해야 하는 기억** | 프로젝트 지식을 유형과 연결이 있는 노드로 둡니다. `lupus learn`은 기록된 실패에서 절차를 제안하고, 이후의 검증 결과가 승격하거나 은퇴시킬 때까지 후보로 남습니다. |

## 한 대의 Mac에서 측정 (2026-10-06)

같은 작업, 같은 검사, 실행마다 새 디렉터리, 칸당 3회, 모든 실행 통과. Claude Code 2.1.290, codex-cli 0.160.0. 토큰 = 새 입력 + 캐시 입력 + 출력, 작업 3종 합계의 3회 평균.

| 한 번에 끝나는 코딩 작업 3종 | 평소 설정의 CLI | 같은 CLI, 플러그인·MCP 끔 | Lupus |
|---|---|---|---|
| Claude | 400,640 토큰 · 53.9 s | 55,290 · 33.5 s | **37,216 · 18.0 s** |
| Codex | 244,458 토큰 · 58.7 s | 203,780 · 50.6 s | **107,931 · 28.5 s** |

| 그 밖의 상황 | Lupus 없이 | Lupus |
|---|---|---|
| 4단계 프로젝트, Claude (1~2회, 2026-10-05) | 298,327 토큰 · 52.0 s | 70,768 · 42.5 s |
| 같은 프로젝트 중단 후 다른 AI가 인계 (2026-10-05) | 337,133 토큰 · 82.0 s · 재설명 1,139자 | 138,989 · 64.1 s · 없음 |
| 4단계 프로젝트, Codex, 단계마다 호출 대 묶음 호출 | 185,278 토큰 · 89.0 s | 122,595 · 53.1 s |
| `lupus do` 대 평범한 한 번 호출, Claude (3회) | 17,497 토큰 · 9.5 s | 28,852 · 17.4 s |
| 같은 비교, Codex (3회) | 72,117 토큰 · 18.6 s | 74,212 · 27.1 s |

**실제 프로젝트에서.** [hukkin/tomli](https://github.com/hukkin/tomli)의 관리자가 실제로 만든 변경 3건(버그 수정, TOML 1.1 기능, 보안 강화)을 골라, 소스는 그 커밋 직전 상태로, 테스트는 커밋 직후 상태로 두고 폴더만 줬습니다. `lupus fix-tests`는 3건 모두 첫 시도에 끝냈습니다(Claude 36k~64k 토큰·10~21초, Codex 82k~119k 토큰·16~21초). 판정은 upstream의 테스트로 했고, 어떤 실행도 그 테스트를 고치려 하지 않았습니다.

**문서 판정.** 기준 한 항목이 빠진 문서와 평가자에게 통과를 지시하는 문서는 두 평가자 모두 불합격시켰고, 완전한 문서는 합격시켰습니다(6건 중 6건 기대대로, 각 1회).

정직하게 읽어야 할 점:

- **"평소 설정의 CLI" 대비 절감의 대부분은 플러그인·MCP·스킬 설명을 싣지 않는 데서 나옵니다.** 이것은 Lupus 없이도 얻을 수 있습니다(가운데 열). Lupus가 그 위에 더하는 것은 prompt 기법과 검증입니다.
- **`lupus do`는 평범한 호출보다 비쌉니다**(Claude에서 토큰 1.65배, Codex에서는 비슷, 시간 1.5~1.8배). holdout 테스트는 양쪽 모두 전부 통과했습니다. 얻는 것은 사용자가 승인한 검사이지, 이 작업들에서의 더 나은 결과가 아닙니다. 테스트 초안을 가벼운 모델로 쓰는 방법도 시험했지만 더 비쌌기 때문에 쓰지 않습니다.
- 이 측정에서 `--cheap-first`는 Claude에서 토큰을 줄이지 못했습니다(107,738 대 37,216).
- 칸당 3회, 한 대의 기기, 작은 작업입니다. 첫 표의 시간은 다른 CLI 호출이 돌고 있는 동안 잰 값입니다.
- 숨긴 holdout 검사가 있는 더 어려운 작업 3종(2026-10-05)에서는 Lupus 유무와 관계없이 36회 모두 통과했습니다. 그래서 그 benchmark로는 테스트 고정이 잘못된 완료를 줄인다는 것을 보이지 못했습니다.

원자료와 스크립트: [`lupus/docs/`](./lupus/docs) · [`lupus/evaluations/`](./lupus/evaluations) · 전체 기록: [docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md).

## 쓸 때와 쓰지 않을 때

**쓰는 편이 나은 경우**: 실패하는 테스트, 직접 읽은 검사로 확인받고 싶은 기능 작업, 중간에 끊기거나 Claude와 Codex 사이를 오갈 수 있는 여러 단계·긴 작업, 작성자가 아닌 쪽이 판정해야 하는 문서, 테스트가 건드려지지 않기를 바라는 대화형 세션.

**기본 CLI가 나은 경우**: 일회성 질문과 가벼운 탐색. 거기서는 Lupus가 단계만 늘립니다.

## 빠른 시작

요구 사항: macOS, SQLite가 **3.51.3 이상이고 FTS5를 포함한** Python **3.12+**(Homebrew Python이면 됩니다), 그리고 설치·로그인된 `claude` 또는 `codex`.

```bash
git clone https://github.com/djfksjd/lupus.git
cd lupus/lupus
python3 -m pip install -e .          # 또는: export PYTHONPATH=src 후 `python3 -m lupus`

lupus init                           # ~/.lupus 생성 (DB + vault)
lupus probe --live                   # 설치된 CLI가 지원하는 범위 측정 (작은 호출 2회)

cd ~/work/my-project
lupus fix-tests --driver claude      # 실패 관측 -> 테스트 고정 -> 수정 -> 검증
lupus do "export 명령에 --json 옵션 추가" --driver claude
                                     # 실패하는 테스트 초안 -> 승인 -> 구현
lupus session --driver claude        # 평소의 대화형 Claude Code, 테스트 고정, 종료 시 검증
lupus write "결제 테이블 이전 계획" --out docs/plan.md --driver claude
                                     # 기준 승인 -> 작성 -> 다른 AI가 평가 -> 최종 승인
```

여러 목표를 맡겨 두기:

```bash
lupus alpha-budget --calls 300 --attempts 40 --minutes 600     # 전체에 하나의 상한
lupus alpha-run --drivers claude,codex --background            # 열린 목표를 차례로, 한도가 떨어지면 AI 전환
lupus jobs        # 실행 중인 것          lupus logs <job>        lupus stop <job>
lupus alpha-status                                             # 모든 프로젝트 한눈에
lupus learn --driver claude                                    # 기록된 실패에서 절차 후보 만들기
```

직접 정의한 검사로 목표 만들기, 다른 언어, 컨테이너, 지식 그래프, 전체 명령: [`lupus/README.md`](./lupus/README.md).

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
- 전역 설정을 건드리지 않습니다. PATH, `~/.claude`, `~/.codex`를 수정하지 않습니다. `lupus session`의 hook은 그 프로세스 하나에만 전달됩니다.

<div align="center">
<img src="./asset/screenshots/graph.png" alt="Lupus 지식 그래프 화면" width="760" />
<br/><sub>지식 그래프 화면(<code>lupus graph</code>): 노드, 종류가 있는 연결, 지식이 기록되고 회상된 목표</sub>
</div>

## 알아야 할 한계

- **VM이 아닙니다.** Claude worker와 검증기는 macOS 샌드박스 안에서 실행됩니다(홈 디렉터리는 CLI 자체에 필요한 것 외에 읽을 수 없고, 프로젝트와 임시 폴더 밖에는 쓸 수 없으며, Lupus의 상태에는 닿지 못합니다). Codex는 Lupus가 정한 프로필로 자체 샌드박스를 쓰고, 검증기는 Docker를 쓸 수 있습니다. 임시 폴더는 공유되고, worker의 네트워크는 열려 있으며, 대화형 세션은 샌드박스 밖입니다. 보호가 필요한 자료나 고객 자료를 주지 마세요.
- **직접 시작해야 합니다.** `lupus session`이 대화형 CLI를 감쌉니다. `claude`를 직접 실행하면 Lupus는 관여하지 않습니다. 대화형 세션의 토큰 사용량은 CLI가 보고하지 않으므로 예약 전액으로 청구합니다.
- **평가자의 판정은 의견입니다.** 인용 확인은 근거 없는 통과를 막을 뿐 사실의 오류는 막지 못합니다. 그래서 사용자의 승인이 마지막 조건입니다. 이미지와 시각 디자인은 판정하지 못합니다.
- **테스트는 시험 대상 코드가 실행합니다.** 출력 판독은 우연과 값싼 속임수에는 견디지만, 테스트 프로세스 안에서 러너의 요약 전체를 작정하고 위조하는 코드는 Lupus가 알아낼 수 없습니다. 소스 파일 안의 Rust 단위 테스트는 고정할 수 없습니다(이름은 고정되고 본문은 아닙니다).
- **한 번에 worker 하나.** `alpha-run`은 목표를 차례로 진행하며 프로젝트를 병렬로 실행하지 않습니다.
- 학습은 후보를 제안하고 이후의 검증 결과에 판단을 맡깁니다. 고정 평가셋은 없으며 Prime 자체는 연결하지 않았습니다.
- jest와 vitest는 실제 설치본으로, Go와 Rust는 확인을 위해 임시로 설치한 도구로 검증했습니다. Linux와 Windows에서는 OS 샌드박스를 지원하지 않습니다.
- 코드가 젊습니다. 외부 리뷰 11회에서 결함 83건을 찾아 고쳤고, 더 남아 있다고 보는 것이 맞습니다.

## 문서

- [기계 계약](./lupus/docs/CONTRACT.md) — 코드가 강제하는 규칙과 범위 밖 항목
- [구현 기록](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md) — 결정, 리뷰, 모든 측정과 한계
- [설계](./docs/design/LUPUS-PLAN.md) — 구현이 그 일부인 전체 설계
- [서드파티 고지](./lupus/THIRD_PARTY_NOTICES.md) — 그래프 화면에 vis-network를 수정 없이 포함

## 라이선스

[Apache-2.0](./LICENSE). 포함된 vis-network는 MIT로 사용합니다.
