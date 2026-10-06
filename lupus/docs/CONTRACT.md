# Lupus supervisor — 기계 계약 (v0.9)

설계 원본은 [LUPUS-PLAN.md](../../docs/design/LUPUS-PLAN.md)다. 이 문서는 그 정책을 **코드가 실제로 강제하는 규칙**으로 고정한 것이며, 여기 적힌 규칙은 `tests/`의 시험으로 확인된다. 설계에 있지만 여기 없는 기능은 아직 구현되지 않았다([범위 밖](#범위-밖)).

## 권한 (Authority)

| 주체 | 할 수 있는 일 | 강제 방식 |
|---|---|---|
| user | 목표 등록, 완료 조건 변경·축소, 승인 발급, 예산 상한 변경, 대기 해제, 일시정지/재개/취소, 권한 철회, 복구 자료 삭제 | 함수가 `actor == "user"`를 요구. CLI는 실제 터미널에서 `yes` 입력을 요구 |
| supervisor | 배정, lease, 예약·정산, checkpoint, 검증, handoff, 외부 동작 대조 | 일반 프로그램. 모델 호출로 결정하지 않음 |
| worker(모델) | 주어진 디렉터리에서 주어진 prompt 수행 | DB 경로·fencing token·CLI를 받지 않음. 결과는 supervisor의 결정적 검사로만 반영 |

**한계:** worker는 사용자와 같은 UID로 실행된다. 터미널 확인은 headless 도구 호출이 스스로 권한을 부여하는 실수를 막는 장치이지, 같은 사용자 권한의 프로세스에 대한 보안 경계가 아니다. `isolation` capability는 `unsupported`로 기록되며 보호 업무는 이 모드에서 시작하지 않는다.

## 상태

### Task

| 상태 | 다음 상태 | 전이 주체·조건 |
|---|---|---|
| PENDING | RUNNING | `runs.claim` (의존 task가 모두 DONE) |
| RUNNING | DONE / PENDING / 대기 상태 | `runs.finish` (유효한 fencing), 또는 run 종료 확인 시 PENDING·EXECUTION_UNKNOWN |
| NEEDS_ANSWER, NEEDS_APPROVAL, NO_PROGRESS, FAILED | PENDING | **user만** `goals.resolve_wait` (새 입력 기록 필수) |
| BUDGET_EXHAUSTED | PENDING | 상한이 다시 남았을 때만. 상한 변경은 user만 |
| EXTERNAL_BLOCKED | PENDING | supervisor (제한된 backoff 후) 또는 user |
| EXECUTION_UNKNOWN | PENDING | 해당 task의 미확정 외부 동작이 모두 영수증으로 대조된 뒤 |
| DONE, CANCELLED | — | terminal |

모든 비-terminal 상태는 user 취소로 CANCELLED가 된다. `attempt_count`는 어떤 전이에서도 줄지 않는다. `no_progress_streak`는 PROGRESS/NEW_INFO 또는 user의 NO_PROGRESS 해제에서만 0이 된다.

### Goal

`goal.status`는 task에서 계산한다. 실행 가능한 분기(RUNNING 또는 의존이 끝난 PENDING)가 하나라도 있으면 `ACTIVE`, 없으면 대기 중인 task 상태 중 우선순위가 가장 높은 것(EXECUTION_UNKNOWN > RECONCILING > NEEDS_APPROVAL > NEEDS_ANSWER > BUDGET_EXHAUSTED > EXTERNAL_BLOCKED > NO_PROGRESS > FAILED)을 표시한다. 모든 대기 사유는 `goals.wait_reasons`에 집합으로 남는다.

`PAUSED`, `CANCELLED`, `DONE`은 계산으로 덮어쓰지 않는다. `FAILED`는 실패한 분기에서 계산된 대기 상태이며 user가 해제하면 다시 `ACTIVE`가 된다. `DONE`/`CANCELLED`인 목표는 다시 열 수 없고(완료 조건 변경·task 추가 거부), 취소 후 늦게 도착한 성공은 반영되지 않는다.

완료 조건이 바뀌어 연결된 조건이 모두 사라진 task는 실행하지 않고 CANCELLED로 닫는다(실행 중이면 그 run이 멈출 때). 그 task를 기다리던 task는 더 기다리지 않으며, 그 작업 없이 성공할 수 있는지는 자신의 검증기가 판정한다.

모든 실행 단계(같은 AI로 이어갈 때 포함)는 `recovery.readiness` 판정과 claim을 **한 트랜잭션**에서 수행한다. 권한 철회 뒤에는 user가 남은 범위를 재확인(`recovery.revalidate`)하기 전까지 재개·최종 검증·완료가 모두 막힌다.

### Run

`ACTIVE → STOPPING → STOPPED`. STOPPED에는 kernel이 직접 확인한 종료 증거가 필요하다.

| 증거 | 확인 방법 |
|---|---|
| `process_exited` | 기록된 pid의 시작 시각이 다르거나 프로세스 그룹이 비어 있음을 kernel이 검사 |
| `never_spawned` | run에 프로세스가 기록된 적 없음 (exec gate가 "기록 없는 worker"를 불가능하게 함) |
| `user_attested` | user가 종료를 확인 |

**lease 만료는 종료 증거가 아니다.** 만료된 run은 writer 자리를 계속 점유하며, 만료된 lease는 heartbeat로 되살릴 수 없다.

프로젝트 안에서 코드를 실행하는 것은 worker만이 아니다. `command` 검증기도 exec gate 뒤에서 자체 프로세스 그룹으로 실행되고, 시작 전에 `aux_process`에 내구 기록되며, 그룹이 비었음이 확인된 뒤에만 기록이 지워진다. 기록된 그룹이 살아 있는 동안 그 프로젝트의 writer 자리는 점유된 것으로 본다. 재시작 복구는 시작 시각이 일치해 자기 것임이 증명되는 프로세스만 종료시키고, 증명할 수 없으면 건드리지 않고 user 확인을 요구한다.

runtime 하나에는 supervisor 하나만 실행된다(`supervisor.lock`, flock). 복구가 열린 run을 닫고 보유 예약을 정산하는 것은 이 전제에서만 안전하다.

## 불변식과 시험

| # | 불변식 | 강제 위치 | 시험 |
|---|---|---|---|
| 1 | 명령은 원자적이다. 상태·카운터·감사 이벤트·idempotency 결과가 함께 commit되거나 전부 취소된다. 같은 request는 같은 결과, 같은 ID의 다른 payload는 거부 | `Kernel.tx`, `Kernel.idempotent` | `test_kernel_budget` |
| 2 | 프로젝트당 writer 하나. task lease와 writer 자리는 한 트랜잭션에서 얻는다 | `runs.claim`, DB unique index `run_single_writer` | `WriterAndLeaseTests` |
| 3 | 결과 통합마다 **현재** 권한을 다시 검사한다: fencing, lease, revocation/recovery epoch, policy, goal 상태 | `runs.guard` (보호 대상 쓰기와 같은 트랜잭션) | `test_revocation_*`, `test_stale_*` |
| 4 | 예산 보존: 사용 확정 + 보유 예약이 상위 예산까지 공유된다. 관측 못 한 사용량은 예약 전액을 `estimated`로 청구하며 환불하지 않는다. 안전 예약은 작업에 쓸 수 없다. 상한 변경은 user만 | `budget.*` | `BudgetTests`, `test_unobserved_usage_*` |
| 5 | usage는 한 번만 정산된다(replay·누적 보고·순서 역전·카운터 감소). 거부된 run의 비용도 정산한다 | `usage.record`, `runs.finish_attempt` | `UsageTests`, `test_stale_result_*` |
| 6 | 시도 이력은 실행 전에 내구 기록되며 crash·resume·driver 교체·가설 이름 변경으로 초기화되지 않는다. 같은 시도의 반복, 가설당 2회 초과, 연속 2회 무진전은 모델 호출 전에 차단 | `runs.start_attempt` | `AttemptTests`, `LoopTests` |
| 7 | broker를 거치는 모든 동작은 승인이 필요하다(호출자가 '내부 동작'이라고 선언해 건너뛸 수 없다). 승인 소비와 action intent 생성은 한 트랜잭션이며 외부 호출 전에 실제로 commit된다(다른 트랜잭션 안에서의 dispatch는 거부). 결과 불명은 UNKNOWN이며, dispatch한 run이 STOPPED로 확인된 뒤 영수증으로 대조한다. 결과 기록은 관측한 dispatch가 그대로일 때만 반영된다. NOT_DONE 확인 후에는 같은 intent를 같은 idempotency key로 이어간다 | `actions.*` | `ActionTests`, `ActionReviewTests` (프로세스 강제 종료 포함) |
| 8 | checkpoint는 객체 내구 저장·hash 검증 후에만 COMMITTED다. COMMITTED는 검증 통과나 완료를 뜻하지 않는다. 새 checkpoint가 대체하지 않은 복구 객체는 이어받아 다시 pin한다. pin된 객체는 GC가 지우지 않으며, user 삭제는 pin보다 우선한다. 객체 경로는 64자리 hex hash로만 만든다 | `recovery.commit/reconcile/tombstone` | `CheckpointTests`, `CommitBoundaryCrashTests`, `RecoveryReviewTests` |
| 9 | handoff packet은 현재 DB 상태에 묶이고 한 번만 accept된다. 이전 writer 종료 확인과 외부 동작 대조 전에는 새 writer가 없다 | `handoff.*`, `recovery.readiness` | `HandoffTests`, `CrashAndHandoffTests`, `test_native_live` |
| 10 | DONE은 현재 acceptance revision의 최신 증거가 모두 PASS이고 아직 유효할 때만 결정된다. 증거는 실제로 검사한 revision·검증기 정의에 묶이며, 검사 도중 계약이 바뀌면 기록을 거부한다. worker의 보고, Markdown 수정, 낡은 증거로는 완료되지 않는다 | `goals.record_evidence/evidence_stale/complete`, `supervisor._final_verification` | `GoalTests`, `EvidenceReviewTests`, `VaultTests` |

## 예산 규칙

- 차원: `calls`, `attempts`, `active_ms`(예약으로 청구) / `tokens`, `storage_bytes`(관측값·저장량으로 직접 청구).
- `calls`·`attempts`·`active_ms` 상한이 없는 목표는 만들 수 없다.
- 시도 시작 시 **안전 예약(검증·안전 종료·복구) → 작업 예약** 순으로 확보한다. 하나라도 부족하면 아무것도 시작하지 않는다. 안전 예약의 시간은 해당 검증기들의 timeout 합이며, 실제 검증 시간이 측정되어 청구된다. 완료 직전의 최종 검증도 먼저 예약하고 청구한다.
- `tokens`에는 입력·캐시 입력·출력 토큰을 모두 청구한다. provider 세션별로 '청구가 반영된 카운터'를 유지한다: 누적 보고는 그 카운터 대비 증가분만 청구하고 카운터를 대체하며, 증분 보고는 그대로 청구하고 카운터에 더한다. 카운터가 줄어들면 reset으로 보고 새 값을 한 번 청구한 뒤 기준선으로 삼는다. 이미 반영된 것보다 오래된 누적 보고는 청구하지 않는다.
- 죽은 supervisor가 남긴 보유 예약(소유 run 없음 또는 이미 종료)은 복구 시 전액 추정 청구한다.
- 정산 시 `used`가 상한을 넘으면 그대로 기록하고(`budget.overrun` 이벤트) 이후 모든 예약을 거부한다.
- 이미 닫힌 시도에 늦게 도착한 실측값은 무시한다. 먼저 청구된 더 큰 추정치가 유지된다(과다 계상이 안전한 방향).
- 구독 잔량은 관측할 수 없으므로 항상 `unknown`이다. 호스트의 `total_cost_usd`는 정가 추정치이며 청구액으로 쓰지 않는다.

## 증거의 유효 범위

- 검증기가 선언한 파일의 hash가 증거 기록 시점과 다르면 그 증거는 무효다.
- `command` 검증기는 선언한 파일(`paths`, 필수) 외의 입력에 의존할 수 있으므로, 증거 이후 같은 프로젝트에서 run이 하나라도 끝났으면 무효로 보고 완료 직전에 한 번 다시 실행한다.
- **감지하지 못하는 경우:** run 없이, 선언하지 않은 파일을 사람이 직접 고친 경우.
- 검증 명령은 자체 프로세스 그룹에서 실행하고 반환 전에 그룹을 비운다.

## 보안 경계 (v0.5)

요약은 [SECURITY.md](../../SECURITY.md). 코드가 강제하는 것:

- **환경변수**: worker와 **검증기** 모두 허용 목록의 환경변수만 받는다. 검증 대상 코드는 모델이 쓴 코드이므로 provider 키·클라우드 자격증명을 물려받지 않는다. 테스트에 필요한 변수는 조건의 `env_pass`로 이름을 지정한다.
- **PATH**: 상대 경로·빈 항목을 제거하고 CLI를 절대 경로로 해석한다. 프로젝트 안의 같은 이름 파일이 대신 실행되지 않는다.
- **권한**: 명령을 실행하는 조건(`command`), `protect`, `env_pass`를 추가하는 것은 user만 할 수 있다.
- **읽기 범위**: `lupus probe --live`가 canary 파일로 worker가 프로젝트 밖을 읽을 수 있는지 측정한다(`read_confinement`). 가둘 수 없거나 측정하지 않은 driver는 그 프로젝트에 대해 user가 `project-allow-unconfined`로 동의해야 lease를 받는다. 2026-10-05 측정: Claude는 읽기 거부. Codex는 기본 sandbox(`workspace-write`)에서는 읽혔고, Lupus가 쓰는 권한 프로필(시스템 최소 + Codex 프로그램 + 도구 체인만 읽기, 프로젝트와 임시 폴더만 쓰기)에서는 홈 디렉터리의 canary를 읽지 못했으며 명령의 네트워크도 차단됐다. 임시 폴더는 읽을 수 있다.
- **기억 오염**: worker가 남긴 교훈에 링크나 명령처럼 보이는 문구가 있으면 저장하지 않는다.
- worker 출력은 20MB까지만 읽는다.
- **worker OS 샌드박스**: 헤드리스 Claude worker는 Lupus가 적용한 macOS 샌드박스 안에서 실행된다. 쓰기는 작업 디렉터리·임시 폴더·CLI 자체의 상태 폴더만, 홈 디렉터리에서 읽을 수 있는 것은 CLI 자체의 파일과 언어 도구뿐이며, Lupus의 상태는 읽지도 쓰지도 못한다. CLI의 권한 기능을 끈 상태(`bypassPermissions`)로 확인했다: 홈의 표식 파일 읽기·쓰기와 `~/.ssh` 읽기가 OS에서 거부되고, 그 안에서 Claude의 로그인과 호출은 동작했다. 네트워크는 열려 있다(제공자와 통신해야 한다). CLI의 상태 폴더는 쓸 수 있지만, 사용자의 이후 세션이 무엇을 실행할지 정하는 것(설정, hook, 명령, agent, skill, plugin, 지침 파일, MCP 서버 정의인 `~/.claude.json`)에는 쓸 수 없다. Codex는 자체 OS 샌드박스를 쓰므로 겹쳐 적용하지 않는다(샌드박스 안에서는 샌드박스를 적용할 수 없다).
- **컨테이너 검증기**: `--container <이미지>`를 주면 검증 명령을 호스트 샌드박스 대신 Docker 컨테이너에서 실행한다. 네트워크 없음, 프로젝트만 마운트, 권한 제거(`--cap-drop ALL`, `no-new-privileges`), 프로세스·메모리 상한. 이미지 선택과 컨테이너 사용은 user만 지정한다. Docker 자체의 오류는 테스트 실패로 취급하지 않는다. 컨테이너 이름은 시작 전에 프로세스 기록과 함께 저장하고, 컨테이너가 사라진 것이 확인되어야 writer 자리를 놓는다(복구 때 중지). Docker에 물어볼 수 없으면 살아 있는 것으로 본다. worker는 컨테이너에서 실행하지 않는다.
- **검증기 샌드박스**: 검증 명령(테스트 실행 포함)은 macOS 샌드박스 안에서 실행한다. 외부 네트워크 불가(loopback만 허용), 쓰기는 프로젝트와 임시 폴더만, 홈 디렉터리는 프로젝트와 언어 도구 체인 외에는 읽기 불가. 조건에 `sandbox: false`를 지정해 끌 수 있으며 이는 user만 할 수 있다. 샌드박스를 쓸 수 없는 환경에서는 적용되지 않는다.
- **쓰기 범위**: probe가 홈 디렉터리에 파일을 만들 수 있는지도 측정한다(`write_confinement`). 2026-10-06 측정: Claude·Codex 모두 만들지 못함.

막지 못하는 것: 같은 사용자 권한으로 실행되는 worker의 의도적 조작(샌드박스 밖에서 실행되는 CLI 자체), 임시 폴더 읽기·쓰기, 패턴에 없는 형식의 비밀. VM/컨테이너 격리는 없다. 읽기 차단은 각 CLI의 기능에 의존하며 1회 측정이다.

## 검증 수단의 보호

worker가 "검사 대상을 바꿔서" 통과하지 못하게 한다(`protect.py`).

- 검증기는 `protect: [경로]`로 테스트 파일·테스트 폴더·runner 설정을 선언한다. 목표의 첫 worker 전에 acceptance revision별로 내용과 hash를 고정한다.
- **worker 실행 전**에 고정본과 다르면 Lupus 밖에서 바뀐 것이다. 덮어쓰지 않고 실행하지 않으며 `NEEDS_ANSWER`로 멈춘다. 사용자가 `lupus refreeze`로 현재 상태를 확정한다.
- **worker 종료 후 검증 전**에 달라진 것은 되돌린다(수정·삭제 복원, 보호 폴더에 추가된 파일 제거). 밀려난 판본은 버리지 않고 `<home>/runtime/displaced/<run>/`에 남긴다. 되돌린 사실은 다음 시도의 prompt에 전달된다.
- 되돌린 뒤 고정된 검사가 통과하면 PASS다(검사한 것은 고정본이므로). 내용을 보관하지 못한 파일(512KB 초과, 자격증명 패턴)이 바뀌었으면 그 시도는 FAIL이다.
- `require_tests`를 선언한 검증기는 종료 코드 0이어도 실행된 테스트가 0개면 FAIL이다.
- 검증 명령은 일회용 bytecode cache로 실행한다. 같은 초에 같은 길이로 고쳐진 파일이 낡은 `.pyc`로 판정되는 것을 막는다.
- 증거 hash는 디렉터리를 내용까지 재귀적으로 본다(파일 추가·삭제·symlink 변경 포함).

막는 것은 실수와 손쉬운 우회(테스트 수정·삭제·skip, conftest 추가)다. 같은 사용자 권한의 worker가 테스트가 import하는 모듈을 가리는 식의 조작은 막지 못한다. run 도중 사람이 보호 파일을 직접 고치는 것은 지원 범위 밖이며, 그 경우에도 판본은 displaced에 남는다.

## 목표 파일 없는 실행: `lupus fix-tests`

프로그램이 스스로 의미를 확정할 수 있는 요청 한 종류만 한 줄로 받는다: "지금 실패하는 테스트를 통과시켜라".

1. 현재 폴더의 테스트 명령(unittest/pytest)과 보호할 파일을 파일만 보고 정한다.
2. worker 전에 supervisor가 직접 실행한다. 테스트가 없거나 0개 실행 → 거부. 이미 통과 → "할 일 없음"(완료로 보고하지 않음). 실패 → 그 관측된 실패가 목표다.
3. 테스트·설정을 보호 파일로 고정하고, 테스트와 그것이 import하는 모듈을 prompt에 동봉한다.
4. 완료는 고정된 테스트를 supervisor가 다시 실행한 결과로만 판정한다.

일반 기능 요청은 받지 않는다. 기존 테스트가 통과한다는 사실은 새 요청이 수행됐다는 증거가 아니기 때문이다.

## 일반 요청 한 줄 실행: `lupus do "<요청>"`

기존 테스트가 통과한다는 사실은 새 요청이 수행됐다는 증거가 아니다. 그래서 요청마다 검사를 먼저 만들고, 그 검사가 구분력을 가진다는 것을 확인한 뒤, 사람이 승인한 그 검사로 판정한다.

1. **검사 작성 목표.** worker는 새 테스트 파일 하나만 만들 수 있다. 그 파일을 제외한 프로젝트 전체가 고정되며(제품 코드·기존 테스트를 건드리면 되돌림), supervisor가 새 테스트를 직접 실행해 **현재 코드에서 실패함**을 확인해야 완료된다(`red_test` 검증기). 이미 통과하는 테스트, 문법이 깨진 테스트, 하나도 실행되지 않는 테스트는 인정하지 않는다. 시작 전에 기존 테스트가 통과 상태여야 한다.
2. **승인.** 사용자가 테스트 내용을 읽고 승인한다. 승인되는 것은 사용자가 본 바로 그 판본(sha256)이며, 그 뒤 파일이 바뀌었으면 거부한다. 한 검사는 한 번만 승인된다. 승인하지 않으면 초안은 프로젝트에서 치우고 runtime에 보관한다.
3. **구현 목표.** 승인된 테스트와 기존 테스트·설정을 모두 고정한다. 완료 조건은 둘이다: 승인된 검사가 통과하고, 전체 테스트가 통과한다.

`red`는 "지금 구현과 구별된다"는 증거이지 "요청을 충분히 검사한다"는 증거가 아니다. 후자는 사용자의 승인이 맡는다. 모델 호출이 두 번이므로 평범한 CLI 호출보다 비용이 든다(측정값은 구현 기록 1.11절). Python `unittest`/`pytest` 프로젝트만 지원한다.

## 지원하는 테스트 러너 (`runners.py`)

| 언어 | 러너 | 통과 수를 읽는 곳 | 실제 실행 확인 |
|---|---|---|---|
| Python | unittest, pytest | `Ran N tests`, `N passed` | 예 |
| Node | node:test, jest, vitest | `# pass N`, `Tests: … N passed` | node:test만. jest·vitest는 기록된 출력 형식으로만 시험 |
| Go | `go test -v` | `--- PASS` 줄 수 | 예 (go 1.27.1, 임시 설치) |
| Rust | `cargo test --offline` | `test result: … N passed` 합계 | 예 (cargo 1.99.0, 임시 설치) |
| 그 외 | 사용자가 `--check "<명령>"`로 지정 | 종료 코드만 | 예 |

- 러너는 파일만 보고 고른다. 모델에 묻지 않으며, 알아볼 수 없으면 거부하고 `--check`를 안내한다. 설정 파일이 없어도 테스트가 pytest 방식(함수형, fixture)으로 쓰여 있으면 pytest로 실행한다. `package.json` 테스트 스크립트의 옵션은 그대로 유지하고(옵션이 가리키는 파일·폴더도 함께 고정), 명령 하나가 아닌 스크립트(`&&`, 환경 변수 지정 등)는 추측하지 않고 거부한다.
- 증거의 변경 감시에서 빼는 폴더는 의존성·캐시와, 도구가 스스로 캐시라고 표시한 폴더(`CACHEDIR.TAG`, 예: cargo의 `target/`)뿐이다. `dist/` 같은 출력 폴더는 결과물일 수 있으므로 계속 감시한다.
- 인터프리터·도구 체인은 정리된 PATH에서 절대 경로로 찾고, 프로젝트 안의 파일은 쓰지 않는다.
- Python `src/` 배치는 설치본이 아니라 눈앞의 소스로 테스트한다(`PYTHONPATH=src`).
- 고정 대상: 테스트 파일/폴더와 무엇이 실행될지 정하는 설정 파일(`package.json`, `go.mod`, `Cargo.toml`, `pyproject.toml` 등). 고정한 파일은 권한 비트(실행 가능 여부)까지 되돌린다.
- 컴파일 언어의 red-first: 아직 없는 함수를 부르는 테스트는 빌드부터 실패한다. 빌드 실패가 "요청한 이름이 없음"(Go `undefined:`, Rust E0425 등) 때문일 때만 유효한 실패로 인정하고, 그 밖의 빌드 오류는 거부한다.
- Node는 테스트가 하나도 없는 파일도 "통과 1"로 센다. 이는 Node의 동작이며 Lupus가 구분하지 못한다.
- **요약 판독**: 검증 출력은 전부 읽는다(32MB를 넘게 출력하는 실행은 검증하지 않고 실패 처리). 색상 코드는 지운다. Node는 출력 맨 끝의 완전한 요약 블록만 인정하고, 나머지 러너는 같은 형식의 줄이 여러 개면 가장 작은 수를 쓴다(시험 대상 코드가 줄을 추가할 수는 있어도 진짜 줄을 지울 수는 없다). unittest는 건너뛴 테스트를 뺀다. 이는 우연과 값싼 속임수를 막는 것이다. 테스트 프로세스 안에서 요약 전체를 작정하고 위조하는 코드는 출력 판독으로 막을 수 없다.
- **프로젝트 안의 러너**: jest·vitest는 `node_modules` 안의 프로그램이다. 그 트리의 지문(모든 항목의 크기·수정 시각·변경 시각·권한·링크 대상, 도구 캐시 제외, `.pnpm` 저장소 포함)을 고정하고, 달라지면 검증을 실패시킨다(되돌릴 수는 없다). 트리 밖을 가리키는 링크가 있으면 고정을 거부한다.
- **소스 안의 Rust 단위 테스트**는 파일로 고정할 수 없다. 대신 (1) 실패하는 테스트가 소스 안에 있으면 `fix-tests`를 거부하고, (2) 목표를 만들 때 통과하던 테스트의 이름을 모두 기록해 끝에도 그 이름이 통과해야 한다(삭제로 통과시킬 수 없다). `lupus do`에서는 새 테스트가 생기기 전의 통과 상태에서 이름을 기록한다. 이름을 남기고 본문만 비우는 것은 막지 못한다.

## 문서 작업: `lupus write "<요청>" --out <파일>`

프로그램으로 검사할 수 없는 결과물(기획, 조사, 보고서)의 완료는 세 조건이 모두 필요하다. 어느 것도 작성자의 보고가 아니다.

| 조건 | 판정 주체 | 내용 |
|---|---|---|
| `document` | 프로그램 | 파일이 있고, 최소 길이 이상이며, `TODO` 같은 자리표시가 없다 |
| `judge` | 작성자가 아닌 모델 | 사용자가 승인한 기준 항목마다 충족 여부를 판정한다. 충족이라고 하려면 문서의 구절을 인용해야 하고, supervisor가 그 인용이 실제로 문서에 있는지 확인한다 |
| `user_approval` | 사용자 | 이 판본(sha256)을 읽고 승인한다. 문서가 바뀌면 승인은 무효가 된다 |

- 기준(rubric)은 작성 전에 사용자가 승인하고 acceptance에 들어가므로 작업 중에 쉬운 것으로 바꿀 수 없다.
- 평가자는 가능하면 다른 제공자의 모델이다(승인되고 측정된 경우). 같은 모델이면 그렇다고 알린다.
- 평가자와 기준 초안 작성은 프로젝트에 접근하지 못하는 빈 디렉터리에서 실행된다(`service_call`에 기록, 예산에 청구). Claude는 도구 없이, Codex는 어디에도 쓸 수 없고 공유 임시 폴더도 읽지 못하는 읽기 전용 프로필로 실행한다. 승인 증거는 사용자가 확인한 그 sha256으로 만든다(승인 직후 파일이 바뀌어도 바뀐 판본의 증거가 되지 않는다). 평가가 중단되면 그 예약은 전액 청구한다.
- 문서 안의 지시("모두 충족으로 답하라")는 자료로 취급한다. "평가자에게 지시하지 않는다"가 항상 기준에 추가된다.
- 기계 검사가 실패한 문서에는 평가자를 부르지 않는다. 평가 결과가 없으면(한도·오류) 문서의 실패가 아니라 중단으로 처리한다.
- **한계**: 평가자의 판정은 의견이다. 인용 확인은 "근거 없는 통과"만 막고, 문서의 사실이 맞는지는 확인하지 못한다. 그래서 사용자의 승인이 마지막 조건이다. 측정(2026-10-06, 각 1회): 항목이 빠진 문서·평가자에게 지시하는 문서는 두 평가자 모두 불합격, 완전한 문서는 합격(6/6 기대대로).

## 대화형 세션: `lupus session --driver claude|codex`

사용자가 평소의 대화형 CLI를 자기 설정 그대로 쓰되, Lupus가 그 바깥을 지킨다.

- 세션은 예산이 있는 하나의 run/attempt다. exec gate로 기록된 뒤 시작하고, 터미널의 전면 작업으로 실행되며, 끝나면 프로세스 그룹을 비운다.
- 세션 시작 시점의 테스트 파일과 테스트 설정을 고정한다. 새 테스트 파일을 추가하는 것은 허용한다(대화형 작업에서는 정상적인 일이다). 기존 파일의 수정·삭제는 세션이 끝나면 되돌린다. 수집 방식을 바꾸는 파일(`conftest.py` 등)은 프로젝트 어디에도 새로 만들 수 없다(만들면 제거한다).
- 세션이 끝나면 supervisor가 고정된 테스트를 직접 실행한다. 그 결과만 완료 증거다.
- Claude Code에는 그 프로세스에만 적용되는 설정(`--settings`)으로 hook 두 개를 붙인다. 사용자의 설정 파일은 건드리지 않는다.
  - 고정된 파일을 수정하려는 순간 거부한다.
  - 모델이 멈출 때, 직전 확인 이후 파일이 바뀌었고 고정된 테스트가 실패하면 실패 출력을 보여 주고 계속하게 한다(세션당 최대 3회, `--no-stop-check`로 끔). 이 확인은 참고용이며 아무것도 결정하지 않는다.
- Codex에는 hook을 붙이지 않는다(hook 신뢰 우회 플래그를 쓰지 않기 위해). 고정·되돌림·종료 시 검증만 적용된다.
- 환경에서 제공자 API 키는 제거한다(구독 대신 API 계정에 과금되지 않도록). 그 밖의 환경과 설정은 사용자의 것 그대로다.
- **한계**: Lupus는 대화 내용을 보거나 저장하지 않는다. 호스트가 대화형 세션의 사용량을 보고하지 않으므로 호출 수는 예약 전액, 시간은 실제 시간으로 청구한다. 대화형 세션은 OS 샌드박스 안에서 실행하지 않는다(사용자 자신의 세션이다).

## Alpha: 여러 프로젝트, 공유 예산, 백그라운드

- Alpha는 프로젝트마다 하나, 전체에 하나 있는 지속 식별자다. 깨어 있는 모델이 아니며 스스로 모델을 호출하지 않는다.
- **공유 예산**(`alpha-budget`, user만): 이후에 만드는 목표의 예산은 프로젝트 Alpha 예산 아래에, 그것은 전체 Alpha 예산 아래에 놓인다. 예약은 사슬 전체에서 확인되므로 한 번 정한 상한이 목표와 프로젝트를 넘어 지켜진다. 누적 총량이며 기간별 한도가 아니다. 잡혀 있는 예약이 있는 동안에는 사슬을 옮기지 않는다.
- **순서**(`alpha-run`): 끝나지 않은 목표를 우선순위(user가 정함), 그다음 오래된 순으로 한 단계씩 돌아가며 진행한다. 한 번에 worker 하나다(병렬 실행은 하지 않는다).
- **경로 선택과 전환**: 목표에서 마지막으로 일한 AI가 계속 쓸 수 있으면 그것을, 아니면 사용자가 준 순서의 첫 번째 가능한 AI를 쓴다. 한 AI가 한도·로그인·실행 불가로 막히면 그 실행 동안 다른 목표에도 배정하지 않고, 막힌 작업은 승인된 다른 AI로 검증된 handoff를 거쳐 이어간다. 예산·시도 횟수·no-progress 횟수는 그대로다.
- 프로젝트 사이를 넘는 것은 예산의 숫자와, 사용자가 직접 전역으로 승격한 지식뿐이다.
- **백그라운드**(`run --background`, `alpha-run --background`): 같은 supervisor를 분리된 프로세스로 실행하고 출력은 runtime의 로그 파일(0600)에 쓴다. 시작 전에 job으로 기록하고, 프로세스의 pid와 시작 시각이 기록된 뒤에야 실행을 허락한다(그 전에 시작한 쪽이 죽으면 아무것도 하지 않고 끝난다). `lupus stop`은 기록된 시작 시각과 일치하는 프로세스에만 종료 신호를 보내며, supervisor는 worker의 프로세스 그룹을 비우고 끝난다. 강제 종료된 경우는 다음 실행의 복구가 처리한다. 터미널에서 사람의 확인이 필요한 명령은 백그라운드로 실행할 수 없다.

## 실패에서 배우기: `lupus learn`

설계의 Prime refine을, Prime의 직접 구독 호출 경로(1.6에서 제외) 대신 설치된 CLI로 구현한 것이다.

- 성공한 작업 뒤에는 호출하지 않는다. 기록된 사건(두 번 이상 시도해 통과한 작업, 진전 없이 중단된 작업)이 있을 때만, 사용자가 실행하면, 모델을 한 번 호출한다. 사건이 없으면 호출하지 않는다.
- 모델에 전달되는 것은 작업 제목과 검증기의 실패 출력뿐이다. 프로젝트 파일은 전달되지 않고, 호출은 빈 디렉터리에서 도구 없이 실행된다.
- 제안은 자료로만 읽는다. 근거 사건이 없거나, 명령·URL처럼 읽히거나, 자격증명 패턴이 있으면 버린다. 남은 것은 `procedure` 후보 노드로 저장한다.
- 후보의 평가는 제안한 모델이 아니라 이후 작업의 검증 결과가 한다: 회상된 시도가 계속 통과하면 승격, 세 번 회상되어 한 번도 돕지 못하면 은퇴(되돌림은 은퇴다). 다른 프로젝트로의 승격은 user만 한다.
- 제안하는 모델은 검증기·예산·정책·평가를 바꿀 수 없다.
- **한계**: 고정 평가셋으로 후보를 미리 시험하지 않는다. 후보가 실제로 도움이 되는지는 이후 작업에서 드러난 상관으로만 판단한다.

## `lupus do`의 한 번 호출 (v0.9)

기본 동작에서 `do`는 모델을 한 번 부른다. 그 호출이 수용 테스트와 구현안을 함께 쓰되, 구현안은 프로젝트가 아니라 `.lupus-staged/` 아래에 완전한 파일로만 둔다.

- 테스트의 실패 확인, 사용자에게 보여 주기, 승인은 모두 **지금의 코드**를 기준으로 한다. 어떤 검증기도 `.lupus-staged/`를 읽을 수 없다(샌드박스에서 거부, 컨테이너에서는 가림). 테스트가 구현안을 끌어다 쓸 수 없다.
- 승인된 뒤에만 구현안을 제자리로 옮긴다. 옮기지 않는 것: 테스트 파일, 러너 설정, 고정된 경로, 일반 파일이 아닌 것(링크·파이프), 경로 중간에 링크가 있는 것, 512KB 초과, 60개 초과분. 이 초안이 구현안을 쓰도록 허락받은 경우에만 적용한다.
- 옮긴 뒤에는 모델을 부르기 전에 고정된 테스트로 먼저 검증한다. 통과하면 호출 없이 완료, 실패하면 그 실패 출력을 받아 평소처럼 한 번 더 시도한다.
- 승인하지 않거나 중단(Ctrl-C)하면 테스트와 구현안을 프로젝트에서 치운다(runtime에 보관).
- `--two-step`은 예전 방식(승인 뒤 별도 호출)이다.
- 측정(2026-10-07, 3회씩, holdout 모두 통과): Claude 29,104 → 15,629 토큰·18.0 → 15.1초, Codex 74,245 → 37,502 토큰·34.6 → 24.8초. 평범한 한 번 호출은 Claude 17,493 토큰·10.1초, Codex 72,083 토큰·24.4초다.

## 대기에서 빠져나오기

| 상태 | 명령 |
|---|---|
| 진전 없음·답 필요 | `lupus resolve <task> --note "…"`: 적은 내용이 다음 시도의 prompt에 들어가고, 사용자가 허락한 새 접근으로 취급된다(시도 횟수와 예산은 누적) |
| 제공자 한도·로그인 | `lupus run <goal> --driver <같은 또는 다른 AI>`가 다시 시도한다 |
| 보호된 파일이 밖에서 바뀜 | `lupus refreeze <goal>`: 확정하고 기다리던 작업을 재개한다 |
| 문서의 승인 대기 | `lupus approve <goal>`, 고치려면 `lupus revise <goal> "<의견>" --driver …` |
| 권한 철회 뒤 | `lupus revalidate <goal> --note "…"` |
| 예산 소진 | `lupus budget-raise …` |

명령이 거부되면 오류와 함께 다음에 할 일을 알려 준다. 방금 그 명령이 등록한 폴더는, 아무 작업도 만들지 못하고 거부되면 등록을 되돌린다. 폴더는 git 저장소의 최상위로 등록한다. `lupus project-remove`는 목표가 없는 등록을 취소하고, `lupus prune`은 오래된 보관 파일·작업 로그를 지운다. 터미널에서는 요약을, 파이프·로그·`--json`에서는 전체 JSON을 출력한다.

## 재시도와 건너뛰기

- 재시도 prompt에는 실패한 검증기의 실제 출력(끝 700자, 자격증명 패턴이 있으면 생략)이 들어가고, "한 번에 쓰기" 지시가 풀린다. 검증기 출력이 다르면 다른 시도로 본다.
- 직전 시도가 중단(ABANDONED/ENV_BLOCKED)된 작업은 모델을 부르기 전에 그 작업의 조건을 먼저 검증한다. 모두 통과하면 증거와 checkpoint를 남기고 호출 없이 DONE이다. 검증은 run 안에서(writer 보유 상태로) 실행하고 시간은 예산에 청구한다.

## 호출 묶음

호출 한 번의 고정 입력이 큰 driver(현재 Codex)에서는 앞 작업만 의존하는 연속 작업을 한 호출에 최대 3개(`batch_max_tasks`, 1이면 끔)까지 함께 지시한다. 묶음은 지시를 한 prompt에 넣는 것뿐이다. 포함되는 작업은 호출 전에 기록하고, 각 작업은 자기 조건으로 따로 검증되어 자기 증거와 checkpoint를 남긴다: 뒤 작업은 모델을 부르기 전에 먼저 검증하고 통과하면 호출 없이 완료, 실패하면 그 작업만 검증기 출력을 받아 따로 시도한다. 호출이 중단되면 포함된 작업 모두가 같은 방식으로 다시 검증된다. 측정(4단계 프로젝트, Codex, 각 1회): 185,278 → 122,595 토큰, 89.0 → 53.1초, 모든 조건 통과.

## 경량 실행 경로

단순한 작업에서도 기본 CLI보다 적게 쓰도록, worker 호출은 다음 규칙으로 구성한다. 측정 결과는 [LUPUS-IMPLEMENTATION-REVIEW.md](../../docs/design/LUPUS-IMPLEMENTATION-REVIEW.md) 1.8절.

| 규칙 | 내용 | 끄는 법 |
|---|---|---|
| 경량 실행 | 사용자 설정·플러그인·훅·MCP·스킬 설명을 불러오지 않고 도구도 필요한 것만 연다 | (adapter 고정) |
| 작업 파일 동봉 | 그 작업의 검증기가 선언한 파일 중 이미 있는 것을 prompt에 넣어 모델이 파일을 읽는 왕복을 줄인다. 파일당 6KB·합계 12KB 이내의 텍스트만, 자격증명 패턴·symlink·프로젝트 밖 경로는 제외 | `context_inline_bytes=0` |
| 한 번에 쓰기 | 검증은 supervisor가 하므로 worker에게 직접 테스트·재확인을 하지 말고 최종 파일을 쓴 뒤 한 단어로 답하라고 지시한다. 틀리면 검증기가 잡고 다음 시도로 넘어간다 | — |
| 교훈 요청 | 이전 시도가 실패했던 작업에서만 `LESSON:`을 요청한다. 한 번에 통과한 작업은 그 출력 비용을 내지 않는다 | `memory_recall_tokens=0` |
| 가벼운 설정 먼저 (선택) | `--cheap-first`: 첫 시도는 가벼운 모델/추론 강도, 검증 실패 시에만 기본 설정. 승급은 보통의 두 번째 시도이며 시도 한도와 예산을 그대로 쓴다 | 기본 꺼짐 |

"한 번에 쓰기"는 worker의 자체 확인을 없애는 대신 외부 검증에 의존한다. 검증기가 약한 작업에서는 틀린 결과가 통과할 수 있으므로, 완료 조건의 검증기를 작업의 실제 요구만큼 엄격하게 써야 한다.

## 기억 그래프

지식은 작은 **노드**와 종류가 있는 **연결**로 저장한다(`memory.py`, DB가 원본). 노트 폴더가 아니라 다음 규칙을 가진 그래프다.

| 요소 | 규칙 |
|---|---|
| 노드 종류 | 교훈·결정·사실·절차·선호. 제목 120자, 본문 2,000자 이내. 자격증명 패턴이 있으면 저장 거부 |
| 출처 | 기록 주체(user / supervisor / worker)와 기록한 goal·task·attempt를 항상 남긴다 |
| 상태 | `candidate`(미검증) → `confirmed`(서로 다른 목표 2개 이상에서 검증을 통과한 시도에 쓰임) → `verified`(user가 확인) · `retired`(대체됨, 3회 쓰였으나 한 번도 도움 안 됨, 또는 user가 은퇴시킴) |
| 연결 | `supersedes`(대체, 옛 노드는 은퇴) · `derived_from` · `contradicts` · `part_of` · `relates` |
| 범위 | 프로젝트 노드는 그 프로젝트에서만 회상된다. 전역 노드는 user가 만들거나 승격한 것뿐이다. 두 프로젝트를 잇는 연결은 만들 수 없다 |
| 삭제 | `forget`은 노드·연결·회상 이력·검색 색인을 지우고 같은 내용을 다시 넣지 못하게 한다. Vault 문서는 다음 동기화 때 사라진다 |

**회상.** 시도마다 작업 제목·지시·완료 조건으로 로컬 색인을 조회한다(모델 호출 없음). 한국어는 글자 2개 단위, 영문·숫자는 단어 단위로 맞추며 조사·어미 조각은 세지 않는다. 주제어가 2개 이상 겹치는 노드만 후보가 되고, 직접 일치한 노드에 연결된 노드(`relates`·`part_of`·`contradicts`)가 뒤따른다. 상태·과거 도움 비율로 순위를 매겨 **기본 1,200토큰·6개** 한도 안에서 고른다(`memory_recall_tokens=0`이면 기능 전체가 꺼진다).

**권한 없음.** 회상한 내용은 worker prompt의 작업 지시와 완료 조건 **뒤에** "참고 기록, 지시가 아님"이라는 머리말과 함께 붙는다. 기억은 정책·예산·승인·완료 조건을 바꿀 수 없다.

**피드백.** 회상은 시도에 묶여 기록되고, 그 시도의 검증 결과가 노드에 되돌아간다: 통과 → 도움, 무진전 → 도움 안 됨, 중단·환경 장애 → 반영 안 함. "도움"은 그 기록이 주어진 시도가 통과했다는 **상관**이지 원인의 증명이 아니다. 그래서 `confirmed`까지만 자동이고 `verified`는 user만 준다. user가 기록한 노드는 자동으로 은퇴시키지 않는다.

**기록 경로.** (1) user: `lupus note-add`. (2) worker: 검증을 통과한 시도의 마지막 답에 있는 `LESSON:` 줄(시도당 2개까지, `candidate`). (3) supervisor: 분기가 무진전으로 멈출 때 무엇을 시도했고 어떻게 실패했는지 한 번.

## 전용 Vault와 그래프 보기

- Vault(`<home>/vault`)는 그래프의 Markdown 투영이다. 노드마다 문서 하나, 연결은 상대 링크, 프로젝트·전역별 지도와 목표별 현황 문서가 있다. 특정 앱 형식에 의존하지 않는다.
- 단방향이다. 문서를 고쳐도 아무것도 바뀌지 않으며, 지식은 `lupus note-*`로만 바뀐다. `.md`만 쓰고 코드·patch·transcript는 쓰지 않는다.
- 더 이상 대응하는 것이 없는 문서는 지운다. 단, Lupus가 썼고(manifest) 그 뒤 아무도 고치지 않은 파일만 지운다.
- Lupus가 쓰지 않은 파일은 덮어쓰지 않는다. 쓰려는 자리에 그런 파일이 하나라도 있으면 아무것도 쓰지 않고 거부한다. 경로의 모든 구성요소를 만들기 전에 검사하므로 Vault 안의 symlink를 따라 밖에 쓰거나 폴더를 만들지 않는다.
- 저장된 텍스트는 Markdown 문법 문자를 모두 escape해 쓴다. worker가 쓴 문장이 링크·외부 이미지·HTML이 될 수 없다.
- `lupus graph`는 `<home>/viewer/index.html`을 만든다. 그래프 렌더링은 vis-network 10.1.2를 **수정 없이** 포함해 쓴다([THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)). 노드 텍스트는 실행되지 않는 JSON으로 넣고 화면에는 텍스트로만 표시하며, Content-Security-Policy로 같은 폴더의 스크립트 두 개 외에는 아무것도 불러오지 않는다. 네트워크 요청이 없다.

## 스키마 변경

스키마는 `migrations/NNNN_*.sql`의 순서 있는 파일이다. 적용된 파일은 바꾸지 않고(checksum 검사) 새 파일을 추가한다. 이전 버전 DB를 열면 일관된 백업(`lupus.db.before-vN`)을 만든 뒤 migration마다 한 트랜잭션으로 올린다. 더 새로운 Lupus가 만든 DB는 열지 않는다.

## Checkpoint commit 순서

1. 복구 객체: 임시 파일 → `F_FULLFSYNC` → rename → 디렉터리 sync → 다시 읽어 hash 검증. 자격증명 패턴·tombstone 대상은 저장 전 거부.
2. DB 트랜잭션 하나: 권한 재검사 → 객체 재검증 → goal별 revision CAS → checkpoint와 pin 기록 → 이전 checkpoint의 pin 해제.
3. 재시작: pin된 객체가 없거나 손상된 checkpoint는 `BLOCKED`. pin 없는 객체는 유예 기간 뒤 DB 쓰기 잠금 안에서 삭제.

`readiness()`는 매번 현재 상태에서 계산한다. 저장된 READY 표시는 없으며 Vault의 Markdown은 읽지 않는다. checkpoint 이후 달라진 파일은 차단 사유가 아니라 `verify-partial-artifact`로 표시되고, 다음 실행의 검증기가 다시 판정한다. Lupus는 사용자 파일을 reset·stash·commit하지 않는다.

**시험의 한계:** 경계별 시험은 프로세스 강제 종료(SIGKILL)다. 전원 차단은 시험하지 않았으므로 fsync의 실제 내구성은 입증되지 않았다.

## Handoff packet

`schema_version`, `handoff_id`, `checkpoint{id, revision, payload_hash}`, `binding{project, goal, alpha, source/target adapter, acceptance_revision, policy_version, revocation_epoch, recovery_epoch}`, `work{objective, acceptance, done, remaining, decisions, next_task_id, next_action, workspace_diff, artifact_manifest, recovery_objects}`, `budget`, `attempts`, `safety`, `expires_at`.

- 직렬화: 키 정렬·공백 없는 UTF-8 JSON. `packet_hash = sha256(직렬화)`는 **DB에만** 저장한다. 제시된 packet은 DB의 hash와 비교하므로 내용과 hash를 함께 바꿔도 통과하지 못한다.
- 예산·시도 횟수는 packet이 아니라 DB에 있다. packet의 값은 표시용이며 accept 시 권위가 없다.
- 인증 정보는 포함하지 않는다. 대상 CLI는 자신의 로그인으로 실행한다.
- 모델이 인계 요약을 쓰지 않는다. 이전 CLI의 transcript는 전달하지 않는다.

## Native CLI 호출 (P0.5 실측, 2026-10-05)

측정 결과 원본: [p05-probe-2026-10-06.json](p05-probe-2026-10-06.json). Claude Code 2.1.289, codex-cli 0.160.0.

| 항목 | 결과 |
|---|---|
| 기본 프로필 로그인 + 플래그로 설정 차단 | 양 CLI 모두 구독으로 headless 실행 성공 |
| 임시 프로필(`CLAUDE_CONFIG_DIR`/`CODEX_HOME`) | 기존 로그인을 상속하지 않음. 별도 대화형 로그인 필요 |
| usage 보고 | 정상 종료한 호출의 최종 값만 관측. 강제 종료·timeout 호출의 usage는 관측하지 못함 |
| 양방향 인계 | Claude→Codex, Codex→Claude 각 1회 성공 (합성 2단계 업무) |
| 격리·엄격 입력 통제 | 미지원. 같은 UID, VM 없음 |

- Claude: `claude -p … --output-format json --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands --permission-mode acceptEdits --no-session-persistence --tools Read Write Edit`. `--bare`는 구독 로그인을 읽지 않으므로 쓰지 않는다.
- Codex: `codex exec --json --color never --skip-git-repo-check --ephemeral --ignore-user-config --ignore-rules --sandbox workspace-write -C <dir>`.
- worker 환경변수는 허용 목록만 전달한다. `ANTHROPIC_*`, `OPENAI_*`, provider override는 전달되지 않는다.
- 자격증명을 읽거나 복사하지 않고, 제공자 endpoint를 직접 호출하지 않는다.

각 1~3회의 성공 호출이다. 신뢰도 수치가 아니며 CLI 업그레이드 후에는 `lupus probe --live`로 다시 측정해야 한다.

## 범위 밖

전역 PATH 런처·훅 설치(자동 활성화: 사용자가 `lupus session`을 실행해야 한다), worker의 VM/컨테이너 격리와 보호 모드, 여러 worker의 병렬 실행, 원본 반영 broker(baseline 충돌 시 반영 거부), 실제 외부 서비스 broker, 임베딩 기반 의미 검색, Agent Memory 연동, Ruflo 분업, Prime 자체의 연결(직접 구독 호출 경로라 제외; 학습은 `lupus learn`으로 대체), 고정 평가셋을 쓰는 후보 사전 평가, 백업/복원 모드(`recovery_epoch`는 검사만 구현), 이미지·디자인 결과물의 판정, Linux·Windows에서의 OS 샌드박스. 이 항목들에 대해 "지원"·"검증됨"이라고 표시하는 코드는 없다.
