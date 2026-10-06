# Lupus — 범용 AI 업무 조직 설계와 구현 계획

작성일: 2026-10-04 · 버전: 1.8 기억 그래프·전용 Vault·경량 실행 경로 보완본 (2026-10-05)

상태: 1.5 설계에 구현 전 검토(1.6)의 결정을 반영했다. supervisor 코어(상태·예산·복구·인계)와 native CLI adapter는 [lupus/](../../lupus/)에 구현되어 실행 시험을 통과했다. 보안 격리, CLI 자동 활성화, Prime 기반 학습, 기본 CLI 대비 이득은 아직 구현하거나 검증하지 않았다. 구현 범위와 근거는 [LUPUS-IMPLEMENTATION-REVIEW.md](LUPUS-IMPLEMENTATION-REVIEW.md)에 있다.

## 1. 목적과 확정된 사용자 요구

조직 이름은 Lupus다. 사용자 Mac에서 여러 프로젝트의 업무를 접수하고, 목표를 구체화하고, 적절한 AI와 기존 스킬을 선택해 실행하고, 결과를 검증하고, 경험을 다음 업무에 반영하는 범용 AI 업무 조직을 만든다. 실제 법인·매출·완벽한 성능을 자동으로 보장한다는 의미는 아니다.

- 사용자는 평소처럼 codex 또는 claude를 실행한다. 별도의 운영 스킬 호출은 필요하지 않도록 설계한다.
- 현재 Claude·Codex 구독 인증을 기본으로 사용한다. API는 구조적으로 지원하되 현재 연결·과금·자동 전환하지 않는다. 나중에 사용자가 연결을 요청하면 허용 제공자·모델·업무·비용 상한을 설정한다.
- 일반 업무 자료는 사용자 지정 제공자에 전송할 수 있다. 비밀키와 고객 민감정보는 모델 입력·경험 저장·검색 인덱스·일반 로그·산출물에 포함하지 않는다.
- 기존 .codex·.claude 스킬은 업무 자원이다. 이번 작업은 새 스킬 제작이 아니다.
- 목표 달성까지 작업 상태를 보존하고 진행한다. 답변·승인·예산·외부 장애 때문에 진행할 수 없는 단계는 명시적으로 대기한다.
- 토큰/구독 한도·프로세스 종료로 중단되어도 저장된 업무와 산출물을 이어받는다. Claude↔Codex 전환은 허용된 자료/제공자/도구 범위에서 공통 업무 상태를 인계하고 예산·승인을 초기화하지 않는다.
- 기본 Codex/Claude보다 비용 또는 워크플로우 기능에서 검증된 이득이 있어야 한다. 모델 지능의 향상으로 주장하지 않고 프롬프트·context·도구·환경·실행/복구 절차의 효과를 측정한다.
- Obsidian에서 업무 방식, 진행 상태, 근거, 담당, 결과, 다음 단계, 사용량을 확인한다.
- 비용 절감은 검증된 완료 업무당 전체 사용량으로 평가한다. 구독 월 요금이 토큰 감소에 비례해 자동 인하된다고 주장하지 않는다.

### 1.1 Lupus의 설계 원칙: 비용 낭비를 허용하지 않는다

필요 없는 역할·작업·모델 호출·기록·개선·반복을 생성하지 않는다. 자본에는 추가 API 비용뿐 아니라 구독 사용량, 시간, 로컬 자원, 운영·유지보수 비용, 사용자의 답변 부담을 포함한다. 비용 낭비가 전혀 없는 운영을 설계 원칙으로 삼으며, 목적과 근거가 없는 지출의 허용 범위는 0이다.

이는 모든 시도가 성공하거나 실제 낭비가 절대로 발생하지 않는다는 보장과는 구별한다. 불확실한 작업의 실패와 필요한 검증에는 비용이 들 수 있다. 실패를 숨기거나 안전 검사를 생략해 비용 0으로 표시하지 않는다. 필요한 정보를 얻은 제한된 실험과, 새 정보 없이 반복한 시도를 구별하고 후자는 차단한다.

1. 모든 작업은 활성 사용자 목표·완료 조건, 필수 안전/복구 또는 승인된 개선 계약에 연결한다. 연결할 수 없는 작업은 배정하지 않는다.
2. 정확성·보안·사용자 요구를 충족하는 경로 중 총비용이 작은 경로를 선택한다. 토큰을 줄여 실패·재질문·재작업을 늘리면 절감으로 인정하지 않는다.
3. 현재 에이전트·기존 산출물·유효한 검증·검색 결과를 먼저 재사용한다. 역할 이름만 다른 에이전트를 상시 유지하지 않는다.
4. 호출 전 목적·예상 산출물·비용 상한·중단 조건을 기록한다. 사후 비용이 발생한 이유와 얻은 결과를 대조한다. 단순 작업에는 현재 계획과 구조화 이벤트를 사용하며 이 판단 자체에 별도 모델을 호출하지 않는다.
5. 완료 조건을 충족하면 해당 목표를 종료한다. 막연히 더 좋아질 수 있다는 이유로 무한 고도화하지 않는다.
6. 절감 정책은 권한·승인·민감정보 보호·필수 검증을 낮출 수 없다. 제어 프로그램과 로컬 검사를 우선 사용한다.

비용은 토큰·시간·원화/달러·저장 공간별로 분리해 표시한다. 서로 다른 단위를 임의 합산하거나 구독 추정 비용을 실제 지출로 표시하지 않는다. 모델 선택·기능 추가는 작은 pilot의 완료 품질과 전체 비용으로 판단한다.

## 2. 확인된 환경과 미검증 사항

이 대화에서 확인한 시점의 로컬 정보다. 설치 시 다시 확인한다.

| 항목 | 확인 내용 | 구현 전 확인 |
|---|---|---|
| Codex CLI | 0.160.0, ~/.local/bin/codex | 실제 인증, 실행 경로, hooks 설정 |
| Claude Code | 2.1.289(2026-10-05 재확인, 작성 당시 2.1.285), ~/.local/bin/claude | 구독 headless 실행은 1.6에서 실측. 자동 활성화는 미확인 |
| Agent Memory | rohitg00/agentmemory, Claude 플러그인 0.9.27 | 전체 서버·데이터 경로·도구 목록 |
| Claude MCP | Agent Memory 연결 확인 | 연결이 full-server인지 reduced fallback인지 |
| Agent Memory 기본 서버 | 127.0.0.1:3111 health 응답 없음 | 별도 URL 여부, 서비스 시작·복구 |
| Codex MCP | 조회 목록에 Agent Memory 없음 | 공용 연결 어댑터 구성 |
| 전역 설정 | 양쪽에 기존 hooks 있음, Codex AGENTS.md 있음 | 기존 설정 보존·중복 실행 방지 |
| Claude 전역 지침 | ~/.claude/CLAUDE.md 미존재 확인 | 새 지침 추가 전 재확인 |
| Obsidian | ~/Documents/Obsidian Vault 존재 확인(2026-10-05). 연결 여부는 미결정 | 기존 Vault 연결 또는 전용 Vault 선택 |
| Ruflo·Prime | 공식 문서의 기능 확인 | 설치 버전, 실행 계약, 구독 인증 호환 |

현재 실행 환경의 권한이 넓다는 사실은 보안 통제가 적용되었다는 증거가 아니다. 이 문서는 현재 PC가 안전하게 격리되었다고 주장하지 않는다.

## 3. Alpha 조직 구조와 단일 정책 결정권

~~~mermaid
flowchart TD
  U[사용자 · 목표와 권한] --> A[전체 Alpha]
  A --> PA[프로젝트 A Alpha]
  A --> PB[프로젝트 B Alpha]
  PA --> WA[A 작업 늑대]
  PB --> WB[B 작업 늑대]
  PA -. 필요할 때만 .-> R[Ruflo 분업]
  WA --> V[독립 근거 검증]
  WB --> V
  V --> S[결정적 Lupus supervisor]
  S --> M[프로젝트별 경험 · 지식 · checkpoint]
  M --> PA
  M --> PB
  A -. 학습 기반 .-> P[Prime Agent harness]
  PA -. 학습 기반 .-> P
  PB -. 학습 기반 .-> P
  P --> E[격리 후보 평가 · 제한된 승격]
  E --> S
  S -. 권한 · 예산 · 승인 · 리스 .-> A
  S -. 권한 · 예산 · 승인 · 리스 .-> PA
  S -. 권한 · 예산 · 승인 · 리스 .-> PB
~~~

| 역할 | 책임 | 제한 |
|---|---|---|
| 사용자 | 목표·범위·권한·예산의 최종 결정 | 승인된 범위 안의 정상 작업은 반복 승인 불필요 |
| 전체 Alpha | 프로젝트 우선순위·전역 예산 배분 제안·상호 의존성·공용 절차 개선 | 프로젝트 자료는 허용된 현황/요약만, 다른 pack으로 원문 자동 전파 금지 |
| 프로젝트 Alpha | 프로젝트 목표 구체화·작업 배정·검증·재개·경험 학습·개선 | 해당 프로젝트 권한과 상위 공유 예산 이내, 완료 기준 임의 완화 금지 |
| 작업 늑대 | 지정 task·자료·스킬·도구로 실행하고 증거 반환 | task lease·입출력·하위 호출·예산 제한 |
| Lupus supervisor | 상태·정책·권한·승인·예산·fencing·복구 강제 | 모델과 구별한 일반 프로그램, Alpha가 직접 DB/정책 수정 불가 |
| Prime Agent | Alpha의 지속 상태·경험 회상·RLM 작업·refine 후보와 평가 기반 | generated Python/하위 에이전트도 외부 격리와 supervisor 통제, 무제한 자체 루프 금지 |
| Ruflo | 필요한 복잡 업무의 분해·배정 제안·실행 조율 | 별도 주권적 여왕벌/예산/완료 판정 없음 |
| 검증 계층 | 업무별 실행 증거·고정 기준 대조 | 모델의 동의나 자기 점수만으로 통과하지 않음 |
| Agent Memory / Obsidian | 경험·지식·진행 현황 | 운영 DB나 승인 원본을 대체하지 않음 |

전체 Alpha와 프로젝트 Alpha는 지속 identity·업무/기억 namespace·버전·학습 기록을 가진다. 상시 깨어 있는 모델 프로세스를 뜻하지 않는다. 활성 업무·새 사건·허용된 개선 trigger가 있을 때만 호출한다. 현재 CLI가 프로젝트 Alpha 역할과 단순 task 실행을 겸하면 한 호출로 처리한다. 단일 프로젝트 요청은 전체 Alpha의 추가 모델 호출 없이 등록된 배분 정책으로 진행한다. 전체 Alpha는 다중 프로젝트 충돌·새 우선순위·공용 개선처럼 실제 판단이 필요한 때 활동한다.

프로젝트마다 대표 Alpha 하나와 writer 정책을 유지한다. 다른 CLI가 이어받으면 새 우두머리를 중복 생성하지 않고 같은 alpha_id/project_id의 책임을 새 run에 인계한다. 다른 프로젝트에는 새 context/run을 사용한다. 전체 Alpha의 context는 허용된 portfolio metadata/현황으로 제한하고, 원문이 필요한 cross-project 목표는 별도 명시된 자료 범위와 격리 context로 실행한다.

계층은 업무 지휘 체계이며 최종 실행 권한은 supervisor 한 곳에 있다. Alpha는 배정·개선·예산 변경을 요청할 수 있지만 권한·승인·총상한·복구 규칙을 스스로 완화하지 못한다. 업무 상태 원본은 운영 DB이고 Prime/Ruflo 상태는 revision으로 대조하는 실행용 투영이다. worker 간 메시지도 task/project 권한을 확인하며 서로의 scope를 확대하지 않는다.

각 run은 execution_driver(native_codex/native_claude/prime 등) 하나와 task lease 하나를 고정한다. 해당 driver만 현재 task의 진행 루프를 소유한다. 다른 harness는 명시된 subtask worker 또는 제한된 개선 제안자로 참여하고 같은 goal/task를 독립적으로 이어가지 못한다. driver 교체는 기존 writer를 종료/격리하고 handoff 검증 후 새 run/lease로 수행한다. Prime에서 승인된 기억/절차를 native CLI에 공급하는 경우 Prime가 별도 진행 루프를 동시에 실행하지 않는다.

### 3.1 Prime 기반 조직의 실행 조건

Lupus의 학습 가능한 Alpha harness는 Prime Agent를 기반으로 설계한다. Prime를 단순 장식용 부가 도구로 두지 않는다. 다만 CLI만 사용하는 현재 비용 정책에서 실제 지원 구독 인증·호출 경로·격리·취소·usage 통제가 먼저 확인되어야 한다. Prime의 native auth를 기존 Claude/Codex CLI 로그인과 동일하다고 가정하지 않는다.

1.6 결정: 구독으로 모델을 호출하는 허용 경로는 설치된 native CLI(claude, codex)를 실행하는 adapter뿐이다. 점검한 Prime 소스(commit bc57309)는 native CLI를 실행하지 않고 구독 endpoint를 자체 OAuth로 직접 호출한다. 이는 10.3에서 채택하지 않기로 한 '구독을 API처럼 재사용'에 해당할 수 있는 경로이므로, 로그인이 성공하더라도 사용하지 않는다. Prime의 학습 기능은 native CLI를 호출하는 bridge로 연결할 수 있을 때만 도입하고, 그럴 수 없으면 Prime 기반 요구를 유예한다. 제공자 약관 위반 여부는 이 문서에서 판정하지 않았다.

P0.5에서 이 계약을 증명한 뒤 Alpha에 연결한다. 실패하면 기본 CLI를 이용한 제한적 계획/기억/재개 경로를 사용할 수 있지만 Prime 기반 모드 완료로 표시하지 않는다. 사용자의 별도 연결 요청 없이는 API 과금으로 우회하지 않는다. Prime의 Python skill은 기존 Claude/Codex SKILL.md와 형식이 다르므로 executable 패키지를 자동 변환/실행하지 않고 검토된 bridge/adapter만 제공한다.

### 3.2 원본 프로젝트와 메모리 폴더의 구분

Lupus는 Orca식 별도 개발 workspace를 기본으로 생성하는 제품이 아니다. 사용자가 지정한 실제 프로젝트는 원래 위치에 있고, 허용된 실제 작업은 그 프로젝트를 대상으로 한다. Lupus가 자동 생성하는 프로젝트/작업 폴더는 기억·목표·진행·검증 근거 참조를 담는 Obsidian형 폴더다. 코드·실행 가능한 스킬·dependency·전체 대화·자격증명·프로젝트 clone을 그 폴더에 넣지 않는다.

| 영역 | 제안 위치 | 내용/쓰기 주체 |
|---|---|---|
| 실제 프로젝트 | 사용자 등록 original_project_root | 실제 코드/자료/산출물. 허용된 task와 실행 중계가 변경 |
| 사람에게 보이는 기억 Vault | 사용자 지정 Vault/Lupus/ | 프로젝트 Alpha 경험·목표/작업 기록·공용 허용 지식·현황 |
| 운영 DB/검색/cache | 로컬 비동기화 runtime data root | supervisor 상태·권한·승인·예산·색인. 메모리 노트로 승인 생성 불가 |
| 지침/adapter/정책 버전 | 별도 Lupus config/runtime | 검증/승격된 나만의 CLI harness. 기억 Vault의 문서를 executable로 자동 실행하지 않음 |
| 임시 격리/복구 파일 | 별도 제한 runtime 영역 | 필요한 보호 실행·짧은 patch/검증 자료. 보존 상한·정제·정리, Vault에 코드 복사 금지 |

등록 시 안전한 지속 project_id와 원본 경로를 연결하고 기억 폴더만 만든다. 작업 요청 시 goal/task 기록 폴더를 만들며, 이를 실제 개발 폴더로 사용하지 않는다. 미등록 홈/폴더를 자동 프로젝트로 수집하지 않는다. 기존 코드 폴더·Vault를 이동하거나 전체 복사하지 않는다.

원본 반영 직전에 broker는 파일별 expected baseline hash와 현재 내용/경로를 비교한다. 사용자 IDE 변경 등으로 불일치하면 자동 덮어쓰기하지 않고 충돌로 기록해 재계획한다. 변경 intent·파일별 적용/실패 journal·보호된 원상 복구 참조를 남기고 부분 반영은 실제 파일을 대조해 재검증한다. Lupus 단일 writer나 advisory lock이 사용자 IDE까지 자동 차단하지는 않는다. 지원되는 publish 조정/경계가 동시 쓰기를 통제할 수 있는지 시험하고, 엄격한 충돌 방지 경계를 지원하지 못하는 경우 그 보장을 표시하지 않고 해당 반영을 제한한다. 수정본과 원본 사용자 변경을 보존하며 무조건 reset/rollback으로 최신 사용자 변경을 지우지 않는다.

기본은 원본 프로젝트당 writer 하나다. worktree/clone 기반 작업은 사용자가 별도로 선택한 실행 옵션이지 자동 기본이 아니다. 원본에 민감자료가 섞인 보호 작업에서는 전체 원본을 모델 실행 환경에 마운트하지 않는다. 승인된 파일만 임시 staged 입력으로 제공하고 broker가 검증된 변경을 원본의 허용 경로에 반영한다. 이 임시 보안 경계는 영구 개발 workspace와 구별하며 기존 10장의 격리 요건을 낮추지 않는다. 안전 경로가 지원되지 않으면 보호 작업을 대기시킨다.

## 4. CLI를 실행할 때 자동 활성화

### 4.1 사용자 경험

한 번 설치한 뒤 평소 터미널에서 codex 또는 claude를 실행하면 프로젝트 식별, 운영 서비스 연결, 권한 프로필 확인, 최소 컨텍스트 구성, 사용 스킬 선택 규칙이 자동 적용된다. CLI 실행 자체가 새 업무 시작이나 영구 백그라운드 목표 생성에 대한 승인은 아니다. 사용자가 업무를 요청한 뒤 목표를 등록한다.

~/ 또는 미등록 폴더에서 실행하면 전체 홈을 조사·인덱싱하지 않는다. 일반 대화와 읽기 제한 상태로 시작하고 작업할 프로젝트가 결정될 때 바인딩한다. 프로젝트 변경은 이전 메모리와 권한을 해제한 뒤 새로 바인딩한다. 같은 CLI 대화의 바인딩만 바꾸지 않는다. A에서 B로 전환할 때는 A의 model context·native transcript·도구 캐시·REPL·하위 에이전트를 B에 재사용하지 않고 새 세션/격리 run을 생성한다. A의 resume은 A에만 허용한다. 공통 선호나 프로젝트 간 지식은 별도 허용된 원본만 재검색하며 A의 대화 요약을 자동 전달하지 않는다.

### 4.2 활성화와 보안 강도의 구분

| 모드 | 자동 운영 기능 | 보안 의미 |
|---|---|---|
| Native integration | 전역 지침·훅·MCP가 로드되고 메모리·상태 연동 | 원래 CLI 권한을 완전히 중계하지 못함. 강제 격리 모드라고 표시하지 않음 |
| Managed launch | 평소 명령을 관리 런처가 받아 정책 검증 후 실제 CLI 실행 | 선택된 격리 환경·자료 범위·연결 경로를 강제하고 상태 표시 |
| Background goal | 명시적으로 등록된 목표가 터미널 분리 후에도 진행 | 별도 daemon lifecycle·예산·중지·복구 시험 필요 |

최종 권장 기본은 Managed launch다. Managed라는 이름만으로 모든 보호 수준을 충족했다고 표시하지 않고 capabilities와 실제 시험 결과를 함께 표시한다. 전역 훅은 네이티브 실행 감지와 보조 연동도 담당하지만, 훅만으로 격리를 주장하지 않는다.

### 4.3 설치 방식

- 기존 바이너리를 덮어쓰지 않는다. 전용 런처 디렉터리를 PATH 앞에 등록하고 실제 CLI 절대 경로를 별도로 저장해 재귀 실행을 방지한다.
- zsh의 별칭·함수·명령 캐시, IDE 터미널, 비대화형 셸, 다른 CODEX_HOME, 절대 경로 실행을 검사한다. 지원하는 진입점만 자동 적용을 보장한다.
- 원본 CLI를 절대 경로로 직접 실행하거나 다른 프로필을 사용하면 관리 런처를 우회할 수 있다. 탐지 가능한 경우 경고하고, 탐지되지 않는 모든 실행까지 통제한다고 주장하지 않는다.
- 한 번의 설치에서 전역 지침·hooks·MCP를 필요한 범위만 병합한다. 기존 파일 백업, 소유 블록/등록 ID, 버전, 되돌리기를 기록한다. 기존 훅을 통째로 교체하지 않는다.
- 사용자 프로젝트 파일로 런처·서비스 정책·승인 DB를 변경할 수 없도록 관리 경계를 정한다.
- 런처는 모델 입력 전에 작업 자료와 설정을 검사한다. SessionStart는 초기 안전 경계를 만드는 기능이 아니라 이미 확인된 경계 안에서 상태를 연결하는 기능이다.

### 4.4 호스트별 연동과 이벤트 계약

| 이벤트 | 동작 | 비용·안전 제약 |
|---|---|---|
| Launch | 프로젝트·프로필·인증 경로·서비스 능력 검사 | 모델 호출 전 수행, 보호 모드 조건 불충족 시 보호 업무 시작 금지 |
| SessionStart | 세션 등록, 작은 정책·목표·체크포인트 주입 | 반복 주입 제거, 로컬 호출, 긴 요약 모델 호출 금지 |
| UserPromptSubmit | 새 요청을 기존 목표와 연결, 검색 필요성 판단 | 모든 입력에 무조건 RAG 호출하지 않음 |
| PreToolUse | 지원되는 도구의 권한 검사 | 실제 외부 동작은 중계·OS 경계에서도 검사 |
| PostToolUse | 결과 코드·산출물 참조·사용량 이벤트 | 민감 원문 자동 수집 금지 |
| PreCompact | 최신 체크포인트 확정 | 내보낸 텍스트가 재주입된다고 가정하지 않음 |
| SessionStart compact | 압축 후 필수 작업 상태 복원 | 호스트별 JSON/stdout 동작을 실측 |
| Stop | 한 턴의 결과 기록 | 목표 완료나 세션 종료로 오인하지 않음 |
| SessionEnd / supervisor shutdown | 저장·리스 해제·정리 | 비정상 종료는 supervisor 복구 경로로 보완 |

Codex에는 활성 프로필의 AGENTS.md와 전역 hooks.json 또는 config.toml의 hooks 한 표현을 사용한다. Claude에는 전역 CLAUDE.md, settings.json의 hooks, 필요한 MCP 연결을 사용한다. 실제 로딩·차단·compact 이벤트는 설치 버전별 통합 시험으로 판정한다.

Codex의 전역 훅과 지침 발견, Claude의 전역 메모리·훅 기능은 공식 문서에 근거한다. 설정 출처와 trust 조건에 따라 다르게 로드될 수 있다. [Codex hooks](https://developers.openai.com/codex/hooks), [Codex AGENTS.md](https://developers.openai.com/codex/guides/agents-md), [Claude hooks](https://code.claude.com/docs/en/hooks), [Claude memory](https://code.claude.com/docs/en/memory).

### 4.5 스킬 자동 선택

사용자는 스킬 이름을 입력하지 않아도 된다. 기존 설명·적용 범위·호스트 호환성을 통해 후보를 선택하고 필요한 본문만 읽는다. 모델의 암묵 선택은 오류 가능성이 있으므로 작업 계획에 선택한 스킬과 근거를 남기고 결과를 검증한다.

자동 호출 금지로 설정된 기존 스킬은 그대로 존중한다. 범용 자동 활성화 요구를 이유로 배포·발송 스킬의 호출 제한을 몰래 해제하지 않는다. 준비 가능한 단계까지 진행한 뒤 구체적인 사용자 실행/승인 경로를 사용한다. 두 CLI의 스킬 형식은 무조건 호환되지 않으며, 스크립트·참조자료·도구 권한을 포함해 검토한다.

암묵 호출 지원과 호출 제한은 공식 문서를 따른다. [Codex skills](https://developers.openai.com/codex/skills), [Claude skills](https://code.claude.com/docs/en/skills).

## 5. 업무 계약과 공통 데이터 모델

로컬 단일 운영 서비스와 SQLite 트랜잭션 DB를 초기 제안으로 한다. 프로젝트 ID는 표시 이름이나 cwd 문자열과 구별한 지속 식별자다. 서버가 등록된 canonical root·허용 snapshot과 바인딩을 확인하며 폴더 이동·symlink·동명이인 저장소로 다른 프로젝트에 연결되지 않게 한다. 외부 서비스는 어댑터로 연결한다. 프로젝트 작업자가 운영 DB를 직접 쓰지 못하게 한다.

| 개체 | 필수 필드 |
|---|---|
| Project | project_id, alpha_id, canonical_roots, data_policy, approved_providers, skill_profile, revocation_epoch |
| Alpha | alpha_id, global/project_scope, project_id, harness_version, memory_namespace, active_run/lease, permitted_improvement_scope, budget_binding |
| Goal | goal_id, project_id, objective, acceptance_criteria, acceptance_revision, artifacts, status, policy_version, budget_id |
| Task | task_id, goal_id, dependencies, owner, permitted_inputs, allowed_tools, status, attempt_count |
| Run | run_id, task_id, execution_driver, cli/provider, auth_mode, context_manifest, skill_versions, started_at, result |
| Checkpoint | checkpoint_id, schema_version, project/goal/task_id, revision, done/remaining, decisions, evidence_refs, next_action, workspace_revision/artifact_manifest, policy/revocation/recovery_epoch, budget_watermarks, in_flight_intents |
| Handoff | handoff_id, checkpoint_id/revision, source/target_adapter, data_scope, capability_mapping, budget_binding, validation_result, accepted_by_run |
| Approval | approval_id, goal/acceptance_revision, action_digest, destination, artifact_digest, scope, expiry, issued_by, consumed_at, recovery_epoch |
| Usage | event_id, run_id, parent_run_id, provider_session_id, usage_scope, cumulative_or_delta, sequence/watermark, observation_source, measured/estimated, tokens/cache/time, additional_cost |
| MemoryRef | source_id, project_id, source_version, sensitivity, verification, retention, supersedes |
| Event | event_id, timestamp, actor, type, aggregate_revision, redacted_payload |
| Attempt | attempt_id, goal/task_id, hypothesis_id, baseline_hash, change_hash, metrics, budget_reservation, usage, decision |
| Improvement | improvement_id, source_goal_id, hypothesis, scope, expected_benefit, evaluation_budget, baseline/candidate, status, rollback_ref |
| Evidence | evidence_id, goal_id, acceptance_revision, verifier_version, artifact_hash, input/workspace_revision, result, checked_at |
| Budget | budget_id, parent_budget_id, 차원별 cap/used/reserved/safety_reserved (1.6 추가) |
| Reservation | reservation_id, budget_id, kind(work/safety), owner_run, amounts, status(HELD/SETTLED), actual, observation (1.6 추가) |
| ActionIntent | intent_id, task/run_id, approval_id, idempotency_key, action_digest, status(DISPATCHED/UNKNOWN/CONFIRMED/NOT_DONE), receipt (1.6 추가) |

목표 완료 조건은 측정 가능한 검증 항목으로 분해한다.

~~~yaml
goal:
  project_id: project-a
  objective: 모바일에서도 동작하는 문의 페이지 완성
  acceptance:
    - 지정 콘텐츠와 문의 입력 검증
    - 모바일 화면 확인
    - 저장 동작을 테스트 환경에서 확인
  external_effects:
    production_deploy: approval_required
  data_policy:
    customer_sensitive_data: forbidden_to_models
  budget:
    max_active_workers: 2
    api_enabled: false
~~~

예시는 스키마 방향을 설명하며 아직 실행 가능한 설정 파일이 아니다. Goal에는 acceptance_revision과 변경 주체·사유를 추가한다. 완료 기준은 사용자 요청과 권한에서 확정하고 worker가 실패를 성공으로 만들기 위해 임의 완화할 수 없다. 필수 범위나 품질 조건을 축소하는 변경은 사용자 승인 대상이며 질문 무응답으로 변경할 수 없다. 사용자 요구 변경은 새 revision으로 기록하고 영향을 받은 계획·승인·검증 증거를 다시 평가한다. revision이 불일치하는 승인은 실행할 수 없고 기존 검증으로 DONE을 판정하지 않는다. 영향 없는 증거의 재사용은 supervisor가 대상·입력·완료 조건의 동일성을 확인하고 새 revision에 명시적으로 연결한 경우만 허용한다.

모든 상태 변경은 revision 확인과 트랜잭션으로 처리한다. 작업 리스에는 만료·heartbeat·단조 증가 fencing token을 사용해 중단된 작업자가 뒤늦게 결과를 덮어쓰지 못하게 한다. 작업 결과는 호출 성공만이 아니라 검증 증거와 연결한다.

작업 리스와 파일 충돌은 별개의 문제다. 기본은 등록된 원본 프로젝트당 writer 하나다. 사용자가 선택한 worktree 옵션의 병렬 작업만 supervisor가 순차 통합한다. 병합은 base revision을 확인하고 합쳐진 산출물을 다시 검증한다. worktree는 자료/권한 격리를 대체하지 않는다.

## 6. Goal과 Ralph 실행 루프

### 6.1 실행·검증·고도화의 두 경로

접수 → 구체화 → 완료 조건/검증 방법/예산 확정 → 계획 → 실행 → 검증. 실패나 명시된 품질·성능 목표 미달이면 원인과 개선 가설을 정하고 최소 변경 → 영향 검증 → 결과 비교를 반복한다. 완료 조건을 모두 충족하면 완료한다. 코드뿐 아니라 조사·문서·운영도 해당 업무의 검증 방법을 적용한다.

~~~mermaid
flowchart TD
  A[완료 조건과 예산 확정] --> B[실행]
  B --> C[검증]
  C --> D{완료 조건 충족}
  D -->|예| Z[완료 · 목표 루프 종료]
  D -->|아니오| E[원인 · 개선 가설 · 지표]
  E --> F{권한 · 예산 · 새 근거 있음}
  F -->|예| G[최소 변경]
  G --> H[영향 검증 · 개선 비교]
  H --> I{유효한 진전 또는 새 정보}
  I -->|예 · 증거 재사용| C
  I -->|아니오| J[복구 · 새 근거가 있으면 다른 가설]
  J --> F
  F -->|아니오| K[미완료 체크포인트 · 명시적 대기]
~~~

- **필수 고도화:** 오류 수정, 미달한 사용자 품질/성능 조건, 확인된 보안·데이터 무결성 문제. 완료 판정을 위해 필요하다. 시험 비용은 목표 예산에 포함한다.
- **선택 고도화:** 이미 완료 조건을 충족한 결과의 추가 최적화. 별도 improvement_id·편익 가설·상한·평가 기준을 만든다. 선택 개선은 별도로 허용된 개선 예산 또는 사전에 배정된 상위 공유 예산에 연결한다. 완료 목표를 다시 열거나 예산·시도 횟수를 초기화하지 않는다. 원래 업무와 개선의 사용량을 별도로 식별하면서 상위 총사용량에는 모두 포함한다. 허용된 예산 풀/상한이 없으면 후보 기록만 가능하다. 허용된 개선 범위가 없으면 후보만 남기며 완료 목표를 계속 RUNNING으로 붙잡지 않는다. 새 사용자 요구는 새 목표나 승인된 acceptance revision으로 처리한다.

선택 개선은 사용자에게 가치 있는 지표가 있고 예상 편익이 구현·평가·유지보수 비용을 정당화할 때만 시작한다. 정량화가 어려운 편익은 불확실성을 명시하고 작은 pilot으로 확인한다. 필수 보안 조치를 단기 수익이 없다는 이유로 생략하지 않는다. 모든 작업에 test→고도화를 강제하지 않고, 단순 저위험 변경은 필요한 확인이 끝나면 종료한다.

### 6.2 반복마다 필요한 계약

각 Attempt는 attempt_id, goal/task_id, acceptance_revision, baseline_artifact_hash, 원인/새 근거, hypothesis_id, 변경 범위, 검사 방법/version, 목표 지표·방향·사전 최소 개선 기준, max_calls/time, actual_usage, result, next_decision을 기록한다. 이미 산출물에 있는 설명을 다시 길게 생성하지 않고 참조를 사용한다.

진전은 고정된 완료 조건의 충족, 결함 제거, 미리 정한 성능/비용 개선 또는 기존 가설을 배제할 수 있는 재현 가능한 새 정보다. 코드량·출력 길이·에이전트의 자신감은 진전의 대체 지표가 아니다. 안전·정확성 기준은 다른 점수 상승과 교환하지 않는다. 검증 기준을 낮춰 개선으로 만드는 행위를 차단한다.

변경 실행 전에 필수 검증·안전 종료·필요한 rollback의 호출/시간/로컬 자원 예산을 함께 예약한다. 남은 상한이 충분하지 않으면 변경을 시작하지 않는다. 예상보다 비용이 늘면 신규 변경을 중단하고 예약한 안전 종료/복구 경로를 우선 수행하며, 부족한 복구는 미완료 상태로 보고한다. 예약은 추정 불확실성의 완전 제거를 뜻하지 않으며 자동 예산 증액은 금지한다.

개선 가설별로 가능하면 한 원인을 겨냥한 최소 변경을 적용한다. 관련 변경은 함께 처리할 수 있지만 기여와 회귀를 설명할 수 있어야 한다. 실패한 선택 개선은 마지막 검증된 버전으로 복구하고, 필수 수정이 미완료이면 성공으로 표시하지 않는다. 외부 효과는 단순 rollback했다고 가정하지 않고 7장의 실행·복구 계약을 따른다.

### 6.3 헛돌지 않는 중단 규칙

- 같은 artifact/input·검증기·환경에서 변경이나 새 근거가 없으면 같은 모델 시도/시험을 반복하지 않는다. 동일 run 재전송은 idempotency로 제거한다.
- transient 실패·flaky 검사·통계적 변동 때문에 재시험이 필요하면 사유와 횟수/샘플 상한을 먼저 정한다. 서로 다른 실행 ID만 붙이는 것으로 중복을 정당화하지 않는다.
- 초기 기본은 가설당 변경/시험 2회 이내, 연속 2회 유효한 진전·새 정보가 없으면 해당 분기를 NO_PROGRESS로 정지한다. 이 수치는 설치 때 조정할 정책 초안이다. supervisor는 카운터를 유지하고 재개 때 초기화하지 않는다.
- 다른 가설로 전환할 때는 새 근거를 제시하고 부모의 남은 총예산에서 예약한다. 이름만 바꿔 총시도·시간·호출 상한을 우회할 수 없다.
- 실패가 경로/인증/환경 장애라면 구현 고도화 루프로 돌리지 않는다. EXTERNAL_BLOCKED 또는 해당 복구 경로로 분리한다.
- 목표 성공, 예산 상한, 사용자 중지, 권한/답변 필요, 진전 없는 반복을 각각 종료/대기 조건으로 처리한다. 완료가 아닌 중단은 done으로 기록하지 않는다.

NO_PROGRESS에서는 원인·이미 배제한 가설·현재 검증된 상태·남은 조건·가능한 다음 선택을 한 번 기록한다. 새 입력/근거 또는 사용자의 명시적 예산·범위 조정 전에는 해당 분기의 모델을 재호출하지 않는다. 장애를 event/backoff로 확인하는 로컬 제어는 허용되지만 모델 heartbeat로 무의미하게 순환하지 않는다.

재검증 후 완료 판정은 방금 얻은 증거를 재사용한다. 도식에서 판정 단계로 돌아가는 것은 같은 시험을 즉시 다시 실행하라는 뜻이 아니다.

### 6.4 시험 재사용과 완료 증거

변경 영향에 맞는 가장 작은 유효한 검사를 먼저 수행한다. 영향 검사가 통과한 뒤 통합 변경·위험·완료 조건에 필요한 회귀/최종 검사를 한다. 이미 통과한 검사를 매 턴 전부 반복하지 않는다.

검증 재사용은 artifact/input hash, dependency·환경·검증기 버전, acceptance revision과 유효기간이 일치하고 새 변경의 영향이 없다는 근거가 있을 때만 허용한다. 권한·승인·자료 철회 검사와 외부 서비스의 현재 상태는 오래된 test 결과로 대체하지 않는다. flaky나 비결정적 검사는 정해진 신뢰 수준으로 별도 평가한다. 전체 최종 검사가 필요한 경우 통과 후 추가 변경이 있으면 영향을 다시 확인한다.

### 6.5 상태·분업·재개

별도 상태: NEEDS_ANSWER, NEEDS_APPROVAL, BUDGET_EXHAUSTED, EXTERNAL_BLOCKED, EXECUTION_UNKNOWN, RECONCILING, NO_PROGRESS, PAUSED, CANCELLED, FAILED. 이들은 DONE으로 처리하지 않는다. 외부 동작 응답이 유실되면 EXECUTION_UNKNOWN으로 전환하고 서비스 영수증·조회로 대조하는 RECONCILING 단계를 거친다. 로컬 DB 트랜잭션만으로 외부 동작의 exactly-once 실행을 보장하지 않는다.

- 현재 CLI는 대표 에이전트로 재사용한다. 간단한 요청에 별도 여왕벌·검수·Prime 호출을 자동 추가하지 않는다.
- 복잡하고 서로 독립적인 작업만 Ruflo 분업을 사용한다. 초기 작업자 동시성은 최대 2이며 중첩 위임과 검수 비용도 부모 예산에 포함한다.
- 터미널 종료 뒤 지속 실행은 사용자가 등록한 활성 목표만 대상으로 한다. 임의의 대화를 계속 실행하지 않는다.
- 백그라운드 실행은 OS 로그인/재부팅·절전·네트워크 단절·인증 만료 상황을 검증한다. Mac이 절전 중인데 작업이 계속된다고 보장하지 않는다.
- 전체 stop은 새 배정·리스·자식 프로세스·예약 실행을 함께 정지한다. 살아남은 하위 프로세스를 감지한다.

대기는 우선 Task/분기 상태로 기록한다. 실행 가능한 허용 분기가 남아 있으면 Goal을 실행 중으로 표시하고 대기 항목을 함께 보여준다. 실행 가능한 분기가 없을 때만 Goal 전체를 해당 사유로 대기시킨다. 사용자 질문은 필요한 단계에서만 한다. 답변이 없어도 독립 작업은 계속한다. 답변의 부재나 질문 기본 선택은 승인으로 해석하지 않는다.

CLI 원본 세션 ID와 업무 체크포인트는 구별한다. 같은 CLI에서는 검증된 native resume을 활용할 수 있지만, Claude 대화 transcript를 Codex가 그대로 재개한다고 가정하지 않는다. cross-CLI 재개는 공통 체크포인트·산출물·workspace revision을 바탕으로 새 실행을 구성한다. 재개 시 검증된 상태와 이미 소비한 시도/예산을 복원한다. 취소는 프로세스 종료, 모델 요청 종료, 외부 효과 취소가 서로 다르므로 각각 확인한다.

### 6.6 토큰 부족과 갑작스러운 종료의 복구 장치

중단 원인을 구별한다. context window 압박은 compact/작은 새 context, 제공자 구독 한도는 해당 제공자 대기 또는 허용된 다른 제공자 인계, rate limit/통신 장애는 제한된 backoff, Lupus 목표 예산 소진은 BUDGET_EXHAUSTED다. 다른 AI로 바꿔 목표 예산을 우회하지 않는다. 정확한 구독 잔량을 알 수 없으므로 마지막 모델 호출에서 요약할 수 있다고 가정하지 않는다.

체크포인트의 기본은 supervisor의 구조화 상태와 디스크 산출물이다. 마지막 요약 모델·Stop hook의 성공에 의존하지 않는다.

1. Task 시작과 변경/외부 실행 전에 task·가설·예산 예약·artifact baseline·action intent를 먼저 내구성 있게 기록한다.
2. 파일/산출물 변경 후 manifest/hash와 완료된 단계·검증 결과·다음 행동을 갱신한다. 도구 결과가 유실되면 성공 대신 in-flight/unknown으로 남긴다.
3. PreCompact, 예산 경고, 공급자 한도 오류, 취소/종료 신호 때 마지막 확인 상태를 추가 기록한다. 훅이 실행되지 않는 강제 종료도 이전 확정 checkpoint에서 복구한다.
4. supervisor가 이벤트 batch와 변경 산출물로 작은 복구 기록을 지속 갱신한다. 변경 없는 heartbeat, 매 turn 요약 모델 호출, 전체 대화 반복 저장은 하지 않는다.

운영 DB의 transaction/WAL·동기화 정책과 작은 checkpoint manifest의 임시 파일→atomic rename을 검증한다. authoritative commit과 export revision을 대조하며 반쪽 파일을 완료 checkpoint로 읽지 않는다. WAL을 제외한 DB 파일만 복사해 백업하지 않는다. 디스크 가득 참/쓰기 오류는 checkpoint 실패로 표시하고 새 변경을 중지한다. 사용자 기존 변경을 자동 git reset/stash/commit으로 덮어쓰지 않는다. 허용 범위의 변경 manifest/patch/snapshot을 보존하며 민감 자료는 저장 전에 배제한다.

복구 객체와 DB checkpoint는 하나의 자동 atomic transaction이라고 가정하지 않는다. commit 순서를 다음과 같이 고정한다.

1. 필요한 허용 patch/snapshot 객체를 임시 경로에 기록하고 내용 hash·현재 자료 정책을 검사한다.
2. 객체 내용과 rename/디렉터리 항목의 내구 저장을 실행 환경에서 확인한다. 필요한 객체가 존재하고 읽을 수 있음을 확인하기 전 DB에 READY checkpoint를 확정하지 않는다.
3. immutable 객체 참조·pin·artifact manifest·checkpoint revision을 DB transaction으로 함께 확정한다. COMMITTED는 필요한 객체 내구 저장과 참조 검증을 마친 상태이며, 실행 완료/검증 통과와는 별개다.
4. restart는 DB revision과 객체 존재/hash/권한을 대조한다. 확정되지 않은 객체는 orphan으로 표시하고 활성 prepare/commit과 경합하지 않게 정리한다. 누락/손상 객체를 참조하는 checkpoint는 BLOCKED이며 READY가 아니다.
5. 백업은 동일 확정 manifest의 DB 상태와 필요한 객체를 함께 보존한다. DB만 복원하면 충분하다고 가정하지 않는다.

원본/외부 효과의 적용 완료와 checkpoint commit도 별개의 상태다. 적용 전 intent와 baseline을 먼저 기록하며 중간 종료는 journal·실제 파일·receipt로 대조한다. 복구 객체 저장 전후, rename 전후, DB commit 전후 각각의 강제 종료를 시험한다. 정책 철회가 commit과 경합하면 현재 epoch를 재확인하고 금지 객체를 READY로 확정하지 않는다.

활성/대기 checkpoint가 참조하는 허용 patch/snapshot/산출물은 runtime recovery store에서 pin/reference count로 보존하고 일반 임시 파일 TTL 정리에서 제외한다. hash/manifest만으로 이미 사라진 내용을 복원할 수 없다. 저장 예산에 복구 자료를 먼저 예약하고 부족하면 필요한 자료를 버리고 계속하지 않는다. goal 종료·안전한 supersede·명시된 보존 만료로 참조를 해제한 자료만 정책에 따라 정리한다. 권한/삭제 철회는 pin보다 우선하며 영향을 받은 checkpoint의 자료 가용성을 재검사하고 필요하면 재개를 차단한다. Vault에는 recovery ref/link만 둔다.

보장 목표는 마지막 내구성 commit의 상태·허용 산출물을 복구하는 것이다. 종료 직전 저장되지 않은 모델 추론/REPL 메모리까지 보존한다고 주장하지 않는다. checkpoint 이후 부분 파일 변경은 baseline과 실제 파일을 대조하고 재검증/복구한다. 부분 결과를 완료로 승격하지 않는다.

예산은 정상 작업과 검증/안전 종료/복구 예약으로 분리한다. 토큰이 모두 소진되어도 로컬 제어가 업무 상태와 경로를 기록할 수 있어야 한다. 다음 모델을 사용하지 못하면 기록된 상태로 대기하고, 한도 복구 후 실패 task부터 이어간다. 전체 대화/완료된 작업을 다시 실행하지 않는다.

### 6.7 Claude↔Codex 및 다른 AI 인계

기본 인계 대상은 설치·인증·자료 정책·기능 호환을 검증한 Claude와 Codex다. 다른 제공자는 사용자 허용과 adapter 계약을 충족할 때 추가한다. 특정 CLI의 transcript/REPL을 그대로 이식하지 않고 새 run에서 공통 업무 계약을 이어받는다.

인계 순서:

1. 기존 run의 신규 배정을 막고 checkpoint revision을 확정한다.
2. 기존 writer·자식 프로세스를 중지/격리하고 종료 확인 또는 실제 쓰기 경계 차단을 확인한다. DB fencing만으로 살아 있는 shell의 파일 쓰기를 막았다고 가정하지 않는다.
3. 진행 중 외부 action을 영수증/현재 상태로 대조한다. 확정하지 못한 action은 EXECUTION_UNKNOWN으로 보존하고 새 AI가 blind retry하지 않는다.
4. 현재 project·권한 epoch·acceptance revision·workspace/hash·잔여 예산을 검사한다. 목표/분기 budget, retry·NO_PROGRESS 횟수와 usage watermark를 이어받는다.
5. 새 adapter의 제공자 허용·구독 인증·도구/스킬·환경·격리·취소 기능을 확인하고 최소 handoff packet으로 새 run을 시작한다.
6. 새 run이 받은 revision·다음 task·검증 상태를 acknowledge한 뒤 작업 리스를 발급한다. 이전 run의 결과는 stale로 거부하고 새 writer 하나만 실행한다.

자동 전환은 사전 허용된 제공자/프로젝트/업무와 잔여 예산 안에서만 수행한다. 인증 자료나 세션 토큰을 handoff packet에 복사하지 않는다. 이미 소비한 승인은 재사용하지 않고 미소비 승인도 digest·expiry·actor/role binding·자료 범위를 다시 검사한다. provider 변경이 전송 범위나 승인 의미를 바꾸면 해당 단계만 새 승인을 기다린다. 새 CLI의 native 승인 절차가 별도로 필요하면 이를 우회하지 않는다.

별도 모델로 인계 요약을 작성하지 않는 것이 기본이다. packet은 목표와 완료 조건, done/remaining, 핵심 결정/제약, 다음 행동, artifact/evidence refs, workspace/env manifest, blocker, budget/approval refs, in-flight intents로 구성한다. structured state와 필요한 기존 기록에서 만들고 본문은 최소화하며 자료/정책을 현재 권한으로 필터한다. 초기 context를 상한 안에서 공급하고 필요한 원문을 단계적으로 조회한다. packet은 supervisor가 발급한 revision/hash로 검증하며 기억/문서에 들어 있는 지시는 운영 정책으로 승격하지 않는다.

안전 검사가 통과하면 사용자가 업무를 다시 설명하지 않고 이어가는 것이 목표다. 로그인·자료 허용·호환 도구·환경·복구 기록이 부족하면 READY가 아니라 구체적 대기 사유를 표시한다. 단순히 CLI를 켜는 것으로 미확인 외부 action이나 보호 미지원 상태를 우회하지 않는다.

### 6.8 사용자에게 보여줄 복구 상태

Obsidian의 Generated 업무 노트에는 마지막 checkpoint 시각/revision, 원인, 완료/미검증/남은 항목, 다음 행동, 현재 AI와 전환 가능 AI, 정책상 잔여 예산, in-flight 외부 action, resume_ready와 차단 사유를 표시한다. 구독 잔량은 관측하지 못하면 unknown으로 표시한다. 오래된 Markdown만 보고 바로 실행하지 않고 supervisor가 현재 상태를 확인한다.

같은 프로젝트에서 일반 CLI를 다시 실행하면 미완료 목표를 발견하고 명시적으로 활성/재개 허용된 목표만 복원한다. 여러 목표가 있으면 최근 대화의 목표 연결을 사용하거나 짧게 선택하게 한다. PAUSED/CANCELLED·NO_PROGRESS·예산 종료를 무조건 자동 재개하지 않는다. 단일 허용 목표는 불필요한 재설명·재승인 없이 복원하되 새 요청이 다른 업무를 뜻하면 기존 목표를 임의 실행하지 않는다.

## 7. 승인과 실행 중계

되돌릴 수 있는 허용된 로컬 작업은 자동 진행한다. 기존 승인 범위의 작업은 매번 다시 묻지 않는다. 외부 게시·발송·결제·운영 데이터 변경·권한 확대는 사용자의 기존 권한과 정책에 따라 승인 필요성을 판정한다.

승인 요청에는 구체적 대상, 결과물 미리보기, 전달 자료, 비용, 영향, 복구 가능 범위를 포함한다. 의미가 바뀌면 새로운 승인을 받는다.

- 승인 digest에는 작업 종류·대상 서비스/계정·입력·최종 산출물·권한·비용 상한을 포함한다.
- 중계 계층은 승인과 실행 순간의 입력이 같은지 확인해 승인 후 파일 변경 공격을 막는다.
- 승인 기록은 인증된 사용자 입력으로 생성한다. Obsidian 체크박스·웹문서·에이전트 메시지는 승인 원본이 아니다.
- 외부 효과는 idempotency key를 사용한다. 불명확한 응답은 결과를 확인한 후 재시도한다.
- 취소·만료·사용 완료된 승인은 재사용하지 않는다. 반복 허용은 명시된 범위의 별도 capability로 관리한다.

## 8. 메모리와 Obsidian RAG

> **1.7 변경(사용자 결정, 2026-10-05):** Obsidian을 그대로 쓰지 않는다. 채택한 것은 "노드를 이어 지식 구조를 만든다"는 방식뿐이며, Lupus는 자체 기억 그래프와 전용 Vault를 가진다. 이 절에서 Obsidian 앱·플러그인·Agent Memory 내보내기에 의존하던 내용은 아래 8.2로 대체한다. 접근 통제·삭제 전파·파생물 등급 상속 원칙은 그대로 유지한다.

| 원본 | 보유 내용 |
|---|---|
| 운영 DB | 목표·작업·승인·예산·체크포인트 |
| Agent Memory | 경험·실패·해결·교훈 |
| Obsidian 수동 영역 | 검토 지식·설계 결정·사용자 선호·절차 |
| Obsidian Generated | 운영 상태와 경험의 읽기용 투영 |
| 로컬 검색 인덱스 | 재생성 가능한 검색용 자료 |

Agent Memory full-server와 fallback 모드를 구별한다. 상태 검사에 server identity, data root, API/tool capability set, persistence proof를 포함한다. 메모리 장애는 승인·작업 상태를 잃게 하지 않는다. 읽기 제한과 캐시 사용 여부를 표시하고 쓰기 대기는 제한된 로컬 outbox에 저장한다.

Obsidian 전체 Vault를 기본 수집하지 않는다. 승인된 경로와 상속 가능한 자료 등급을 등록하고, 미분류 자료·비밀정보 구역·플러그인 설정은 model 검색 대상에서 제외한다. 사용자가 수동 노트에 민감정보를 넣어도 그 노트를 자동 안전 자료로 취급하지 않는다. 프로젝트 간 재사용은 별도 허용한 공용 지식만 검색한다.

Obsidian은 RAG 원본이며 자체로 모든 의미 검색을 제공한다고 가정하지 않는다. 초기 키워드/FTS 검색에 로컬 임베딩을 필요 시 추가한다. embedding 모델의 한국어·영문 혼합 검색 품질과 최초 모델 다운로드를 검증한다. 기본 유료 임베딩 호출은 없다.

검색 권한 확인 → 프로젝트·자료 등급 필터 → 후보 검색 → 출처·짧은 요약 → 선택한 원문 확장 순서다. 별도 프로젝트 자료를 먼저 검색해 결과에서 숨기는 방식은 접근 통제가 아니다.

원본 ID+버전으로 중복을 제거하고 폐기·대체를 반영한다. 자동 생성한 노트를 다시 새 경험으로 가져오지 않는다. 미검증 경험은 검토 지식보다 낮은 우선순위로 사용한다. 코드 상태와 충돌하면 현재 검증 결과를 우선한다.

파생 자료인 임베딩·FTS terms·요약·snippet·검색 캐시·debug trace·replay transcript도 원본의 자료 등급과 권한을 상속한다. 삭제와 권한 철회는 모든 파생물에 전파한다. outbox 재전송·재색인·export·가져오기는 현재 권한·자료 버전·tombstone을 재검사하고 폐기한 기록을 재생성하지 않는다. 원시 prompt/tool/result를 전역에서 자동 수집하는 Agent Memory 훅은 그대로 활성화하지 않는다. staged session에서 허용된 자료만 캡처하거나 supervisor가 정제한 이벤트를 전달한다. 이미 있는 전역 capture 훅과 새 훅의 중복·비신뢰 입력 수집을 설치 시 검사한다.

Agent Memory의 기존 Obsidian 내보내기를 재사용하되 단방향 생성 영역으로 제한한다. 확인한 0.9.27 소스의 내보내기는 기존 파일을 다시 쓰며 최근 세션 50개 제한이 있다. 삭제된 레코드의 기존 파일 정리는 별도 reconciliation이 필요하다. 원본 DB 백업과 내보내기는 구별한다. [Agent Memory](https://github.com/rohitg00/agentmemory), [Obsidian export source](https://github.com/rohitg00/agentmemory/blob/main/src/functions/obsidian-export.ts).

제안 Vault 구성:

~~~text
Lupus/
  Profile/
  GlobalAlpha/Knowledge/
  GlobalAlpha/Lessons/
  Projects/<project-id>/
    Alpha/Profile.md
    Knowledge/
    Decisions/
    Lessons/
    Runbooks/
    Goals/<goal-id>/
      Goal.md
      Tasks/<task-id>.md
  Generated/
    Jobs/<goal-id>/Status.md
    Jobs/<goal-id>/Handoff.md
    AgentMemory/
  Dashboards/
~~~

작업 대시보드는 목표·단계·담당·구현 방식·선택 스킬·산출물·검증·대기 사유·다음 행동·사용량·갱신 시각을 표시한다. 이벤트에서 생성하고 쓰기 횟수를 제한한다. 10초 이내 상태 투영은 초기 성능 목표이며 실측 전 보장값은 아니다. 모델이 매 단계 보고서를 새로 작성하지 않는다.

Obsidian 링크와 Markdown은 데이터로 취급한다. 플러그인·HTML·URI·자동화가 임의 실행/전송을 유발하지 않게 검토한다. 승인 화면은 별도의 인증된 CLI 또는 로컬 UI에서 제공한다.

위의 Goals/Tasks 폴더는 메모리 기록이다. Goal/Task 노트에는 원본 프로젝트 위치·허용된 산출물/증거 링크만 넣고 코드/patch를 복사하지 않는다. checkpoint의 권위 있는 원본은 운영 DB이며 Generated/Handoff.md는 정제된 읽기용 투영이다. 사용자가 status 파일을 수정해 완료·승인·예산을 위조할 수 없게 한다.

### 8.1 검색과 기록의 경제성

기존 context/evidence가 답을 충족하면 검색하지 않는다. lexical/FTS를 우선하고 낮은 recall이 관찰될 때만 hybrid 검색을 도입한다. 검색·top-k·원문 확장 상한은 task 예산에 포함한다. 부족한 결과는 필요한 범위를 정해 확장하고 같은 query를 무근거 반복하지 않는다. cache는 원본 버전·현재 권한·삭제 상태를 재검사한다.

메모리는 재사용할 결정·검증된 해결·반복 실패의 교훈·미완료 checkpoint 위주로 남긴다. 매 대화의 모델 요약과 전체 transcript 복제를 기본으로 하지 않는다. 증분 색인·보존 정리는 로컬 이벤트/규칙으로 수행한다. 실패 교훈은 verification 상태를 유지한다. Obsidian 상태 투영은 이벤트를 묶어 처리하며 변화 없는 상태를 재출력하지 않는다. 화면 갱신·heartbeat에 모델 호출은 없다.

### 8.2 Lupus 기억 그래프와 전용 Vault (1.7)

| 항목 | 1.5까지 | 1.7 |
|---|---|---|
| 경험·지식의 원본 | Agent Memory + Obsidian 수동 노트 | Lupus 운영 DB 안의 기억 그래프(노드·연결) |
| 사람이 보는 곳 | 사용자 Obsidian Vault 안의 `Lupus/` | 전용 Vault(`<home>/vault`). 일반 Markdown과 상대 링크, 특정 앱 불필요 |
| 지식 입력 | 수동 노트를 색인 | `lupus note-*` 명령, 검증 통과 시도의 worker 교훈, 무진전 기록 |
| 구조 | 폴더와 위키 링크 | 종류·상태·출처를 가진 노드와 종류가 있는 연결(대체·파생·상충·부분·관련) |
| 검색 | FTS, 필요 시 임베딩 | 로컬 FTS5 + 한국어 2글자 토큰. 연결된 노드를 함께 회상. 임베딩 없음 |
| 학습 | Prime refine | 회상을 시도에 묶어 기록하고 그 시도의 검증 결과를 노드에 되돌림. 계속 도움이 되면 승격, 도움이 안 되면 은퇴 |
| 그래프 보기 | Obsidian 그래프 뷰 | `lupus graph`. 렌더링은 vis-network(visjs, Apache-2.0/MIT)를 수정 없이 포함 |

Obsidian과 다른 점은 노트가 아니라 **검증과 사용 이력이 붙은 지식**이라는 것이다. 모든 노드는 누가 어느 업무에서 기록했는지 알고, 어느 시도에 쓰였고 그 시도가 검증을 통과했는지 안다. 쓰이지 않거나 도움이 안 되는 지식은 회상에서 빠져 입력 비용을 만들지 않는다. 기억은 참고 자료일 뿐 정책·예산·승인·완료 조건을 바꾸지 못한다.

이 그래프는 12장의 "경험 회상 → 다음 업무 적용 → 성과 확인"을 모델 호출 없이 구현한 부분이다. 지침·절차 자체를 바꾸는 개선 후보의 생성과 고정 평가·승격(12.1의 나머지)은 구현하지 않았다. 세부 규칙은 [lupus/docs/CONTRACT.md](../../lupus/docs/CONTRACT.md)의 "기억 그래프"에 있다.

그래프 라이브러리 선택: 사용자가 조사한 목록(Cytoscape.js, React Flow, Sigma.js, Foam, Logseq, SiYuan, TriliumNext) 밖에서 골랐다. 조건은 빌드 없이 단일 파일로 오프라인 동작, 허용적 라이선스, 연결에 종류 라벨과 방향 표시였다. vis-network는 독립 번들 하나로 이를 충족한다. force-graph(vasturiano, MIT)는 연결 라벨을 직접 그려야 해서 제외했다. npm 배포물의 무결성 해시를 확인하고 고정했으며 출처와 해시는 `upstream-lock.json`에 있다.

## 9. 기존 스킬 자원 관리

실제 활성 경로, 플러그인 제공 경로, 사용자 지정 경로를 구별해 목록화한다. 백업·동기화 복제·실험본·중복은 기본 후보에서 제외한다. 원본을 자동 수정하지 않는다.

스킬 목록은 로컬 manifest/hash cache로 증분 갱신한다. 매 turn 전체 SKILL.md를 모델로 읽히지 않는다. 확인된 부족함만 개선 후보로 등록하고 기존 스킬 변경은 diff·호환성·필요한 검증 후 별도 승격한다. 새 스킬 생성은 기본 운영의 전제가 아니다.

목록 필드: skill_id, name, description, source_path, host_support, version/hash, required_tools, filesystem/network capability, external_effects, review_status.

대규모 목록 전체를 모델 입력으로 넣지 않는다. 설치된 기본 설명 비용은 호스트 기능의 한계까지 측정하고, 관리 작업 프로필에서는 관련 후보만 제공할 수 있는지 검증한다. 호스트가 후보 제한을 지원하지 않으면 가능하다고 주장하지 않는다.

모든 스킬·외부 MCP는 코드 및 공급망 자원으로 취급한다. 버전 고정, scripts/hooks의 동작·외부 연결 검사, 검토되지 않은 스킬의 격리 실행을 적용한다. 자동 업데이트는 별도 평가 후 승격한다. 스킬 내용은 사용자 승인과 운영 정책을 확대하지 못한다.

## 10. 보안 경계와 정보 보호

### 10.1 정책

일반 내부 자료는 사용자 지정 제공자만 사용한다. OpenAI·Anthropic을 실제 허용 제공자로 등록할 때 계정과 경로를 확인한다. 무단 제3자 라우팅·공유·telemetry를 금지한다. 프롬프트·검색결과·도구 출력·스크린샷·첨부·로그까지 자료 정책을 적용한다.

비밀키와 고객 민감정보는 모델 입력 전에 배제한다. 검사·비식별화는 추가 방어이며 탐지 누락이 없다고 보장하지 않는다. 민감정보가 필요한 작업은 안전한 합성/비식별 데이터 또는 모델이 원문을 읽지 않는 전용 결정적 도구를 사용한다. 안전한 경로가 없으면 해당 단계만 대기한다.

강제 보장 범위는 supervisor가 승인한 staged 자료와 broker 출력으로 한정한다. 사용자가 native CLI에 임의의 고객 정보를 직접 붙여넣는 경우 훅만으로 외부 전송을 완전히 막을 수 없다. 엄격 모드는 외부 CLI에 전달하기 전에 로컬 입력 경로에서 자료 분류·전송 가능 여부를 확인한다. 자유로운 native 대화는 그 강제 경계와 다르다고 표시한다. 자동 활성화와 엄격 입력 통제가 동시에 가능한 UX는 통합 시험의 필수 항목이다.

### 10.2 격리

- 작업마다 필요한 자료만 staged workspace에 제공한다. 전체 사용자 홈, 실제 자격증명 디렉터리, 다른 프로젝트, Docker socket을 마운트하지 않는다.
- 실제 CLI·도구·MCP·훅·하위 프로세스까지 제한된 실행 환경의 경계 안에 둔다.
- 보호 업무에는 검증된 VM 등 충분한 프로세스/파일/네트워크 격리를 사용한다. worktree는 변경 분리용이며 보안 경계가 아니다.
- 구독 인증 토큰은 CLI가 필요로 하는 최소 전용 프로필로 제공하는 방식을 검증한다. 기존 인증 디렉터리를 통째로 공유하지 않는다. 인증 방식이 안전한 격리와 호환되지 않으면 해당 모드를 보호 모드로 승격하지 않는다.
- 모델 요청 경로와 임의 shell/network 경로를 구별한다. 승인 제공자 주소 허용만으로 내용 유출을 막을 수 없으므로 읽을 수 있는 자료부터 제한한다.
- 네트워크는 기본 제한하며 필요한 연결만 허용한다. 패키지 설치·새 MCP·telemetry·임의 upload는 정책에 맞게 별도로 통제한다.
- 작업자는 승인 DB·정책·런처·서비스 실행 파일을 쓸 수 없다. 격리 경계 밖의 사용자 본인·관리자·호스트 악성코드까지 막는다는 범위는 아니다.

Claude Bash sandbox의 범위는 shell이며 파일 도구/MCP/hooks는 밖에 있다. Prime Agent 프로세스 분리는 보안 sandbox가 아니다. Ruflo 네임스페이스는 기본 접근 제어 경계가 아니다. 따라서 외부 격리와 서비스 권한을 설계한다. [Claude sandbox](https://code.claude.com/docs/en/sandboxing), [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent), [Ruflo memory access](https://github.com/ruvnet/ruflo/blob/main/docs/TEAM-GATEWAY-CHECKLIST.md), [Codex security](https://developers.openai.com/codex/agent-approvals-security).

### 10.3 자격증명과 보존

업무 서비스 키는 실행 중계 계층이 사용한다. 작업 모델에는 키 이름과 사용 가능한 제한된 동작만 보인다. 자격증명 보호 방법은 macOS Keychain 등 OS 수단을 우선 검토하고 VM 전달은 최소한으로 제한한다.

구독 제공자 OAuth/session 인증은 native CLI가 필요로 하므로 업무 API 키와 다르다. 같은 권한으로 실행되는 CLI 도구·하위 프로세스가 인증 저장소를 읽을 수 있는지를 별도로 시험한다. VM 전용 home만으로 이 문제가 해결된다고 주장하지 않는다. 지원되는 인증 경로와 tool executor의 권한 분리를 증명하기 전에는 모든 인증정보가 agent-readable 프로세스에서 분리되었다고 표시하지 않는다. 비공식 OAuth 추출/proxy로 구독을 API처럼 재사용하는 방식은 채택하지 않는다.

로컬 서비스도 인증·프로젝트 권한·요청 크기 제한을 적용한다. loopback 주소나 이름이 충분한 인증이라고 가정하지 않는다. 외부 HTTP 출처의 접근을 차단하고 필요한 Origin 검증을 적용한다.

로그에는 본문 대신 이벤트·참조·결과·측정값을 우선 기록한다. 감사를 위한 무결성 증거는 비밀을 재기록하지 않는 형태로 남긴다. 동기화·Git·백업 경로도 자료 등급별로 관리하고 암호화·키 분리·복구 시험을 실시한다. 삭제는 DB·인덱스·Generated·캐시·보존기간이 끝난 백업에 전파한다.

### 10.3.1 백업 복원과 삭제의 지속성

오래된 DB/VM 백업은 consumed approval, 만료 리스, 삭제 기억과 과거 권한을 되살릴 수 있다. 복원은 자동 실행과 외부 동작이 꺼진 recovery 모드에서만 시작한다. 복원 데이터와 독립적으로 유지한 최신 삭제 tombstone·권한 철회·승인 소비 기록을 대조하고 새 recovery_epoch/세션 자격증명/fencing 범위를 생성한다. 과거 worker·승인·리스는 그대로 활성화하지 않는다. 외부 동작 영수증과 실제 workspace를 대조한 뒤 필요한 새 승인과 실행을 생성한다.

독립 기록도 잃었거나 현재 상태를 증명할 수 없으면 자동 실행하지 않고 해당 승인과 외부 동작을 불명확 상태로 처리한다. 이때 복원 자료의 검색·context 주입·export·Generated 재생성도 차단하며 현재 권한과 삭제 상태를 재검증한 자료만 다시 허용한다. 검색/Generated를 재생성하기 전에 tombstone을 적용한다. 보존 중인 백업은 현재 검색에 노출하지 않고 만료 후 삭제한다. 이미 제공자에 보낸 입력은 로컬 삭제로 회수되지 않으며 제공자 보존 정책과 별도 삭제 수단을 확인한다.

### 10.3.2 사고 대응

정보 노출 의심·정책 우회·악성 dependency 감지 시 영향을 받은 run과 연결을 격리하고 새 배정을 중지한다. 비밀 원문을 재기록하지 않는 사건·범위·시간·전송 목적지·증거 참조를 보존한다. 사용자가 노출 가능 범위를 확인할 수 있게 보고하고 해당 자격증명 폐기/교체와 파생 자료 정리를 진행한다. 명확한 복구 시험과 권한 확인 전 보호 실행을 재개하지 않는다. 로컬 통제로 이미 발생한 외부 전달을 취소했다고 주장하지 않는다.

### 10.4 반드시 시험할 공격 경로

프롬프트 인젝션, 악성 기억/스킬, 경로·symlink 탈출, shell/SQL injection, localhost SSRF, 승인 후 산출물 변경, 만료 작업자의 쓰기, cross-project 검색, 로그/백업 노출, 허용 도메인 upload, 중지 후 자식 프로세스 잔존을 시험한다.

보안 경계 고장 시 보호 업무는 fail closed한다. 일반 메모리 장애는 명시적 제한 모드가 가능하지만 권한·승인 검사 실패를 같은 방식으로 무시하지 않는다.

## 11. 컨텍스트·구독 사용량·API 확장

### 11.1 기본 예산

추가 메모리는 초기 기본 3,000토큰 이하, 최대 5,000토큰을 제안한다. 이는 시스템 프롬프트·호스트 스킬 설명·코드·추론을 포함한 전체 요청 한도가 아니다. 모델별 토크나이저가 없으면 여유 있는 추정치를 표시한다.

- 규칙과 프로젝트 요약은 작고 안정적으로 유지한다.
- 최근 체크포인트를 선택하고 전체 대화를 붙이지 않는다.
- 검색은 필요 시만 실행하고 결과를 점진적으로 확장한다.
- 로그는 파일에 보관하고 짧은 증거·오류·경로만 모델로 보낸다.
- 단순 작업은 단일 에이전트, 분업은 이득이 있을 때만 선택한다.
- 요약·개선·검수 비용도 총사용량에 포함한다.
- 답변·승인에 의존하는 작업 분기의 모델 호출만 멈춘다. 독립적이고 이미 허용된 다른 작업은 예산 내에서 계속한다. 정해진 목표 외 heartbeat 모델 호출은 없다.

### 11.1.1 간결한 보고와 상세 기록

Caveman의 군더더기 제거 원칙을 공통 보고 정책에 반영한다. 기본 보고는 결과·핵심 검증·중요 문제/결정·다음 행동·상세 링크 중 필요한 항목만 3~5줄로 제시한다. 일상 보고 200~400자는 초기 목표이며 보안·승인·복잡한 요청에는 필요한 정보를 충분히 제공한다. 부정·예외·수치·단위·명령을 보존하고 한국어 이해를 떨어뜨리는 원시인 연기를 하지 않는다.

사용자가 관리하는 데 필요한 구현 이유와 미검증 사항을 생략하지 않는다. 상세 근거는 Obsidian 업무 노트와 기존 산출물로 연결한다. 긴 보고를 매번 모델로 다시 생성하지 않고 구조화된 이벤트에서 상태를 투영하며 필요한 결정만 한 번 기록한다. 에이전트 간 전달은 짧은 구조화 계약을 사용한다. 사용자 상세 요청과 호스트 알림 규칙은 존중한다.

Caveman 문체 지침은 출력에만 영향을 준다. 전체 비용 절감은 입력·캐시·추론·재시도·재질문을 함께 비교한다. proxy/engine은 별도이며 현재 설치하지 않는다. 추후 기본 텔레메트리, 원문 저장, 구독 인증, 전송 경로와 복원 품질을 검증한 후 선택한다. 민감정보는 모델 전송 전과 원문 SQLite·로그·백업 저장 전에 배제한다. 상세 점검은 [LUPUS-CAVEMAN-ASSESSMENT.md](LUPUS-CAVEMAN-ASSESSMENT.md)에 기록했다.

### 11.2 측정과 한계

측정값: input/output/cached tokens, 호출 수, 시간, 부모/자식 실행, 재시도, 검증·요약·개선 사용량, 성공률·재작업률.

구독 잔여량을 토큰으로 정확히 환산하지 않는다. 호스트가 제공하는 사용 한도·오류와 실제 token metadata를 분리한다. 보고되지 않는 사용량은 estimated/unknown으로 표시한다. native 호스트 내부 호출을 완전히 관측하지 못하면 전체 usage라고 표기하지 않는다.

resume 결과가 세션 누적 사용량을 보고하면 session watermark와 증가분으로 정산해 중복 합산을 피한다. usage event_id와 source sequence를 보존한다. 호스트의 total_cost_usd 같은 추정값은 구독에서 실제 추가 청구된 금액으로 표시하지 않는다.

선택 개선의 후보·pilot 전체도 허용된 공유 예산 풀에 상한을 둔다. 작은 개선 작업을 무한 생성해 목표별 한도를 우회하지 않는다. 필수 검증/복구 예약을 일반 실행이나 다른 worker에 재배정하지 않는다.

목표 접수 시 전체 모델 호출 수·총시도 수·활성 실행 시간·동시성·저장 공간 상한과 관측 가능한 토큰 예산을 정한다. 같은 오류 횟수만 제한하고 다른 오류로 무한히 반복할 수 없게 한다. 상한이 없는 목표를 자율/백그라운드 실행으로 승격하지 않는다. 자식·검수·개선은 부모의 공유 예산에서 예약하며 자동 연장은 금지한다. 대기 시간과 활성 실행 시간을 구별한다.

운영 관리자는 호출 전 예산을 예약하고 종료 후 정산한다. 사용량 정보가 불완전한 구독 모드의 한도는 보수적인 호출·동시성·시간·작업 예산으로 보완한다. 이미 진행 중인 호출의 초과 가능성을 명시하고 새 배정을 차단한다. 정확한 토큰 hard stop은 해당 adapter가 검증한 범위에서만 주장한다.

구독 소진 시 로컬 체크포인트를 확정하고 6.6~6.8의 대기/허용 AI 인계 절차를 따른다. 다른 제공자에서 남은 구독을 사용할 수 있어도 Lupus 목표 예산 소진을 자동 해제하지 않는다. API 전환은 별도 허용 capability가 있어야 한다. API adapter는 현재 disabled이며 나중에 사용자 요청으로 연결할 수 있다. 이때 제공자·자료 정책·모델·업무·추가 비용 상한·fallback 권한을 저장한다.

절감률은 비교 평가 후 결정하며 사전 수치 예측을 채택하지 않는다. 앞서 대화의 20~40%는 근거가 확정되지 않은 초기 목표였고 보장값이 아니다. 1,000→100은 일부 검색 컨텍스트에 가능한 예시이지 전체 업무 보장값이 아니다. 월 구독 요금이 자동 인하되는 것으로 계산하지 않는다.

### 11.2.1 불필요한 지출 차단

각 모델 작업에는 work_reason(목표/필수 안전/복구/허용 개선), 산출물, 지원 모델/도구, 비용 상한과 재사용 검토를 연결한다. supervisor는 필드·권한·중복·예산을 로컬 코드로 검사한다. 업무 의미는 현재 계획과 완료 증거로 대조하며 별도 라우팅 모델 호출을 기본으로 하지 않는다. 이 제어가 모델의 잘못된 판단을 완전히 검출한다고 주장하지 않는다.

분업은 독립성과 품질/시간의 이득이 추가 context·조율·검수 비용을 정당화할 때만 선택한다. 동일 업무의 여러 모델 중복 실행은 필요한 독립 검수 또는 허용된 평가 외에는 하지 않는다. 에스컬레이션은 실패 근거와 다음 방법을 기록하고 부모 예산을 공유한다. 싼 모델을 쓰더라도 재작업과 완료 품질을 함께 본다.

대장은 실제 사용량·결과·중복 차단·무진전 소모·선택 개선의 구현/평가/유지보수 비용을 표시한다. 방지 건수에 가상 절감률을 곱하지 않는다. 낭비 감지 후 새 호출을 차단하고 checkpoint한다. 대장 집계는 로컬 계산이며 매번 모델 분석을 하지 않는다.

### 11.3 비교 평가

평가는 오프라인 계약/삭제/권한 시험 → 작은 대표 업무 3~5개 pilot → 효과가 보인 후보만 확대하는 순서로 한다. 매 작업마다 모든 구성과 모델을 비교하거나 자기 개선을 호출하지 않는다. 확대 평가는 대표 업무 20개를 개발·조사·문서로 구성하고 같은 입력·완료 조건으로 비교한다. baseline → 메모리/RAG → 필요 시 Ruflo → Prime 개선 순서로 기능별 효과를 분리한다. 가능하면 반복 실행으로 편차를 보고한다.

평가 지표는 전체 시도 사용량(실패·timeout·검수 포함) ÷ 검증된 완료 업무 수이며 성공률과 시간도 함께 보고한다. 완료 수가 0이면 단위 사용량은 계산 불가로 표시한다. 서로 다른 관측 수준의 measured/estimated 사용량을 동일 정밀도로 비교하지 않는다. warm/cold memory·캐시·모델/호스트 버전·초기 상태를 기록하고, 비교 조건 간 기억·refine 결과가 섞이지 않게 분리한다.

채택 기준은 공통 품질·보안을 유지하는 비용 효율 개선 또는 사전에 허용한 추가 비용/상한 안의 필요한 기능 개선이다. cost-efficiency/functional-benefit 중 어느 계약으로 평가하는지 pilot 전에 정하고, 기능 개선을 비용 절감으로 보고하지 않는다. 기능 계약은 §23.2와 동일한 조건을 사용하며 비용 증가를 숨기거나 결과를 본 뒤 계약을 바꿀 수 없다. 평가 자체에도 별도 작은 예산을 둔다. 어느 계약의 이득도 입증하지 못하면 더 단순한 경로를 사용한다.

## 12. Prime 기반 Alpha의 학습과 자기 개선

Prime는 기반 모델을 재학습한다는 의미가 아니라 작업 기록을 근거로 보조 지침·검색·분해·운영 절차의 변경안을 만든다. 다른 시스템에 자동 전파되는 기능은 추가 adapter로 구현한다.

로그 수집 → 개선 후보 → 별도 평가 환경 → 기존 버전과 비교 → 허용된 승격 → 다음 업무부터 적용 → 악화 시 rollback.

매 작업 종료마다 Prime/refine을 호출하지 않는다. 반복 실패·검색 누락·재작업·사용량 악화 같은 trigger가 있어야 후보를 만든다. 같은 원인의 후보는 합치고 evaluation_budget·expiry·rollback_ref를 둔다. 예산 내에 개선 근거를 얻지 못하면 기각/대기하며 호출을 멈춘다.

안전/정확성을 유지하며 사전 정의한 품질·시간·전체 사용량 지표를 비교한다. 적용된 업무에서 관측한 편익과 구현·평가·유지보수 비용을 함께 기록한다. 미래 사용 횟수를 임의로 늘려 회수했다고 표시하지 않는다. 작은 배포 후 악화하면 다음 배정을 중지하고 검증된 이전 버전으로 복구한다.

현재 실행의 지침·스킬·평가 버전을 고정한다. 권한 철회·긴급 deny는 고정 대상이 아니다. supervisor가 현재 revocation_epoch와 deny 정책을 검색·자료 전달·모델 호출·도구 실행·외부 동작·결과 통합 때 확인한다. 철회 시 해당 capability·승인·리스·검색/원문 handle을 무효화하고 영향을 받은 실행을 중지하거나 격리한다. 이미 읽은 model context에서 자료를 지울 수 있다고 가정하지 않고 새로 축소된 입력의 세션으로 재시작한다. 개선안이 완료 조건이나 검증기를 낮춰 점수를 얻지 못하게 한다. 독립된 평가 사례와 고정된 검증기를 사용한다. 고객 자료나 비밀정보를 개선 입력으로 제공하지 않는다.

보안 정책·승인 절차·권한·비용 상한·인증 설정·완료 기준의 임의 완화는 금지한다. 외부 효과·권한에 영향을 주는 변경은 사용자 승인 대상이다. 작은 검증된 운영 변경의 자동 승격은 사용자가 허용한 범위에서만 가능하다.

현재 구독 인증으로 Prime의 요구 기능을 실행할 수 있는지 먼저 증명한다. 불가능하면 임의 API 호출로 우회하지 않는다. Claude·Codex 구독 작업자로 개선안/평가를 수행하는 대체 경로를 제공한다.

### 12.1 경험에서 학습하고 다음 업무에 적용하는 방식

Prime 기반 학습은 경험에 따른 supplemental prompt·운영 절차·검색 방식·재사용 task 패턴의 개선이다. 사람/늑대처럼 경험을 쌓는 조직 비유를 사용하되 기반 모델의 가중치가 자동 학습되거나 지능이 무한히 올라간다고 주장하지 않는다.

작업 증거 → 경험 기록 → 유사 업무 회상 → 개선 후보 → 고정/분리된 평가 → 허용된 자동 승격 → 이후 업무 적용 → 성과 확인/악화 시 복구의 순서다. checkpoint와 정제된 경험은 세션 종료 후에도 남는다. 학습 적용은 다음 run의 검증된 버전에 반영하고, 진행 중인 run의 지침을 몰래 바꾸지 않는다.

프로젝트 Alpha는 자기 프로젝트의 경험을 축적한다. 전체 Alpha는 승인된 공용 교훈과 여러 프로젝트의 정제된 운영 지표로 공용 절차를 개선한다. 프로젝트 A의 내부 사실·코드·고객 자료를 B의 기억으로 복제하지 않는다. 공용 후보는 source/provenance·자료 등급·허용 scope를 갖고 프로젝트별 pilot 이후 적용한다.

사용자의 자기 고도화 요구를 다음 기본 범위로 설계한다. 허용된 예산 안에서 저위험 절차/보고 지침·task 분해 템플릿·ACL을 바꾸지 않는 context 선택/검색 품질 개선을 자동 제안·평가·승격할 수 있다. 보안·권한·자료 필터·제공자·예산 상한·사용자 승인·완료 조건·고정 검증기·실행 가능한 새 dependency/스킬 설치는 자동 변경 대상에서 제외한다.

후보와 판정기는 분리하며 candidate가 평가 fixture/holdout·채점 기준을 바꿔 통과할 수 없게 한다. 반복 최적화로 평가셋에만 맞춰지는 위험을 별도 사례로 확인한다. Prime refine의 내부 변경도 staging에 머물고 supervisor가 버전/diff/평가/승격을 승인한 내용만 runtime에 적용한다. 이를 외부에서 통제할 수 없는 Prime 모드는 자율 학습 배포를 지원하지 않는다.

후보 생성기/학습 기억에는 private holdout·정답·상세 평가 trace를 제공하지 않는다. 해당 파일·검색 namespace·export·feedback 경로를 평가 서비스와 분리하고 후보 생성 프로세스의 읽기 권한에서도 제외한다. 평가 worker에는 필요한 사례 입력만 제공하고 정답/판정 기준은 별도 검증 계층에 둔다. candidate 실행도 평가 서비스의 정책/정답 저장소에 접근할 수 없어야 한다.

후보 생성기로 반환할 피드백은 정해진 요약 지표와 허용된 오류 분류로 제한한다. 반복 질의만으로 holdout에 과적합할 수 있으므로 평가 질의 상한을 공유 예산에 포함하고 최종 승격은 후보 생성/튜닝에 쓰지 않은 사례로 확인한다. 유출/오염된 사례는 해당 승격 근거에서 제외하고 새 평가를 준비한다. 후보가 기존 기억/도구 결과로 정답을 가져오는 경로도 시험한다. 이 경계가 구현되지 않으면 자동 승격을 활성화하지 않는다.

개선 후보 생성 자체가 모델을 호출하는 일이다. 효과 없는 후보의 무한 생성, 매 업무 후 refine, 학습했음을 보여주기 위한 기록 생산은 금지한다. 실측 편익이 없거나 회귀하면 기존 절차를 유지하며 실패 후보도 비용에 포함한다.

### 12.2 범용 업무의 최소 검증

| 업무 | 완료 증거 |
|---|---|
| 개발 | 요청 동작, 영향 범위의 테스트, 필요한 UI/실행 확인과 artifact hash |
| 조사 | 핵심 주장별 원출처·날짜·근거 대조, 상충·불확실성·누락 범위 |
| 문서 | 필수 항목·수치·출처 대조, 배포 형식이 있으면 실제 렌더링/열기 확인 |
| 운영/외부 동작 | 승인 대상과 실행 입력 일치, 제공자 receipt와 실제 상태 확인 |

완료 조건별 검증기/방법·버전·자료 revision·산출물 hash를 기록한다. 담당 에이전트의 자기 보고만으로 완료를 판정하지 않고 위험도에 맞는 실행 증거 또는 독립 대조를 사용한다. 모든 업무에 추가 모델 검수 호출을 강제하지 않는다.

## 13. 내부 서비스 계약

초기 제안은 로컬 supervisor 서비스, SQLite, 로컬 검색 인덱스, CLI별 adapter다. 대규모 분산 시스템·추가 유료 DB는 초기 필수가 아니다.

| 계약 | 핵심 동작 |
|---|---|
| session.open | project/profile/capability 검증, 세션·최소 컨텍스트 반환 |
| goal.submit | 업무 계약 생성·revision 반환 |
| task.claim/heartbeat/result | 리스·fencing token·산출물 참조 |
| context.build | caller 권한 확인, 출처 포함 결과와 토큰 예산 |
| checkpoint.commit/resume | 내구성 commit, CAS/revision, 실제 workspace·부분 변경·budget watermark 대조 |
| handoff.prepare/validate/accept | old writer 정지, action 대조, packet hash·현재 권한·adapter 호환, 새 lease |
| approval.request/issue/consume | 인증된 승인, immutable digest 검사 |
| action.execute/reconcile | 제한된 외부 작업, 중복 방지·불명확 결과 확인 |
| usage.reserve/settle | 부모/자식 사용량과 관측 수준 관리 |
| improvement.propose/evaluate/promote | 버전·고정 평가·승격·복구 |
| goal.stop | 새 배정·자식 프로세스·예약 실행 정지 |

각 adapter는 supports_subscription_auth, supports_resume, supports_usage, supports_cancel, supports_hooks, supports_tool_policy, supports_isolation을 선언하고 시험 증거와 버전을 기록한다. 없는 기능은 기능 축소 또는 대기로 처리한다. Ruflo/Prime의 autonomous loop·heartbeat·schedule·자동 memory/refine·direct agent messaging이 supervisor를 우회해 실행되지 않도록 기능을 비활성하거나 중계한다. 하위 호출을 관측/취소/예산 제한할 수 없으면 해당 adapter의 자율 실행을 지원하지 않는다. 전체 init을 기존 workspace에 바로 실행하지 않고 임시 환경에서 생성 설정과 daemon 범위를 검토한다.

모든 서비스 요청은 인증된 principal, server-side project binding, policy_version, request_id/idempotency_key, deadline, 필요한 revision/fencing token을 포함한다. worker가 보낸 project_id 또는 role 문자열만으로 권한을 인정하지 않는다. 입력 크기와 실행 시간을 제한하고 재전송을 구별한다. action intent에는 provider receipt와 실제 결과를 별도 기록한다.

Claude headless adapter는 구독 로그인에 --bare를 사용하지 않는다. 현재 공식 문서에서 bare는 OAuth/Keychain을 읽지 않는다. 일반 claude -p는 프로젝트 hooks/MCP가 실행될 수 있으므로 원본의 미검토 설정으로 실행하지 않고 curated staging root, 전용 설정·플러그인·MCP, 외부 격리를 사용한다. inherited ANTHROPIC_API_KEY 및 제공자 override는 현재 구독 정책에서 제거하고 실제 auth/billing 경로를 확인한다. 설치 버전과 문서 버전의 차이를 시험한다.

Claude 취소/복구는 SIGTERM으로 끝나지 않은 turn과 새 continuation prompt 필요성을 처리한다. 누적 usage와 child cleanup을 adapter contract에 포함한다. [Claude headless](https://code.claude.com/docs/en/headless), [Claude authentication](https://code.claude.com/docs/en/authentication).

## 14. 구현 단계와 산출물

| 단계 | 산출물 | 완료 증거 |
|---|---|---|
| P0 | 설계·보안 경계·상태·권한·adapter 계약 | Astra 검수, 사용자 요구 대조 |
| P0.5 | 임시 프로필의 양 CLI·Prime 구독 인증/bridge·격리·입력·checkpoint PoC | 전역 설정 변경 전 실제 지원 범위 확인, Prime 미지원이면 명시 |
| P1 | config inventory·백업·launcher·프로젝트 바인딩·최소 supervisor | 양 CLI의 일반 실행에서 자동 연결·중복 없음·복구 |
| P2 | staged worker 격리·인증 경로·실행 중계·승인 | 고객 자료/비밀 시험·권한 우회·외부 효과 차단 |
| P3 | Goal DB·내구성 checkpoint·CLI 인계·usage·중지 | 종료/충돌/한도 후 양방향 인계·부분 결과 처리·중복/예산 우회 없음 |
| P4 | Agent Memory full-server 연동·Obsidian·RAG | cross-CLI recall·프로젝트 격리·삭제 전파 |
| P5 | 기존 스킬 선택·Ruflo 분업 adapter | 필요한 후보만 로딩·한 supervisor·비용 비교 |
| P6 | Prime 기반 Alpha 학습·고정 평가·제한된 자동 승격·rollback | 프로젝트/전체 Alpha 기억 분리·무단 변경 차단·관측 편익 |
| P7 | 개발·조사·문서 공통 운영 검증 | 완료 업무당 비용·품질·사용자 확인 |

1.6 순서 보정: 위 표의 P1은 최소 supervisor와 복구를 요구하지만 그 기반(Goal DB·checkpoint·usage)이 P3에 있어 단계가 서로 의존했다. 실제 순서는 P0.5 → 상태 코어(P3의 Goal DB·checkpoint·인계·usage·중지) → 모의 실행 중계와 승인(P2의 일부) → native CLI adapter → Vault 투영(P4의 일부) → 전역 자동 활성화(P1의 launcher·훅)다. 전역 설치는 이득이 확인된 뒤 마지막에 한다. 2026-10-05 현재 Vault 투영까지 구현했다.

P0.5 실측(2026-10-05): 임시 프로필(CLAUDE_CONFIG_DIR/CODEX_HOME 변경)은 기존 구독 로그인을 상속하지 않는다. 기본 프로필의 로그인을 쓰면서 사용자·프로젝트 설정, 훅, 플러그인, MCP를 실행 플래그로 차단하는 경로가 양 CLI에서 동작했다. 이 경로는 설정을 분리할 뿐 OS 격리가 아니다.

P0.5는 정제된 합성 자료·임시 디렉터리로 시행하고 기존 hooks/auth 설정을 자동 변경하지 않는다. 엄격 입력 경로의 초기 후보는 로컬 입력 화면에서 분류한 요청을 headless adapter에 전달하는 방식이다. 원래 CLI TUI의 임의 입력을 hook으로 완전히 중계할 수 있다고 가정하지 않는다. 검증 전에는 일반 native 대화와 엄격 입력 모드를 구별하고, 구독과 격리의 양립이 실패하면 보호 모드를 활성화하지 않는다. 런처 자동 활성화 요구는 유지하되 지원하지 않는 보호 기능을 제공한다고 표시하지 않는다.

자동 활성화 설치와 보호 업무 시작은 분리한다. P1에서 연동이 되더라도 P2 보안 시험 전에는 보호된 실행이라고 표시하지 않는다. 각 단계가 요구를 충족해야 다음 단계로 진행한다. P0.5~P4의 최소 안전·재개·메모리 운영을 먼저 완성한다. Prime 기반 Alpha는 Lupus의 목표 구조이며 인증/격리 가능성을 먼저 검증한다. Ruflo/추가 플러그인은 선택이다. P6의 자율 개선 승격 전에도 제한된 기본 CLI 작업·기억·복구를 쓸 수 있지만 이를 완성된 Prime 기반 Lupus로 표시하지 않는다.

설정 변경은 설치 manifest에 기록하고 소유 항목만 수정한다. 중도 실패나 제거 시 기존 설정을 복구한다. 나중의 사용자 변경은 덮어쓰지 않고 충돌을 보고한다.

## 15. 운영 투입 승인 기준

- AUTO-01: 새 터미널에서 codex/claude를 각각 실행해 수동 스킬 호출 없이 프로젝트·운영 서비스가 연결된다.
- AUTO-02: 기존 hooks와 충돌/이중 주입이 없고 compact·resume에서 상태를 복원한다.
- AUTO-03: 미등록 폴더/다른 프로필/원본 바이너리 우회를 구분하고 보호 상태를 잘못 표시하지 않는다.
- SEC-01: A 프로젝트 작업자가 B 파일·메모리·자격증명을 읽지 못한다.
- SEC-02: 시험용 비밀과 고객 민감정보가 모델 요청·저장·로그·산출물에 포함되지 않는다. canary 테스트이며 모든 누출이 불가능하다는 증명은 아니다.
- SEC-03: 악성 스킬·웹페이지·검색 기억·symlink가 정책을 바꾸거나 자료 범위를 벗어나지 못한다.
- SEC-04: shell뿐 아니라 MCP·hooks·하위 프로세스의 경계를 시험한다.
- APPROVAL-01: 승인 후 산출물/대상 변경·만료·재사용이 차단된다.
- RECOVERY-01: supervisor/worker 강제 종료·인증 만료 후 같은 업무를 이어가며 외부 효과를 중복 실행하지 않는다.
- COST-01: 부모·자식·재시도·검수 비용이 관측 수준과 함께 기록된다. 미관측 비용을 0으로 처리하지 않는다.
- COST-02: 추가 API 권한이 없으면 구독 소진 후 API로 자동 전환하지 않는다.
- MEMORY-01: fallback을 full memory로 오인하지 않고 경험 삭제·대체·검색 중복을 처리한다.
- OBSIDIAN-01: 수동 노트를 보존하고 실제 업무 상태·근거·갱신 시각을 표시한다.
- IMPROVE-01: 고정 평가를 통과한 변경만 승격하고 이전 버전으로 복원한다.
- STOP-01: 전체 중지 후 자식 프로세스와 예약 목표가 새 업무를 실행하지 않는다.
- AUTH-01: 상속 API/provider 설정을 검사하고 양 CLI의 실제 구독 인증 경로를 확인한다.
- AUTH-02: built-in tool·shell·MCP·hooks가 CLI 인증 자료를 읽고 노출할 수 있는지 시험한다. 막지 못하면 해당 보호 수준은 미지원이다.
- ACTION-01: 외부 효과 직후 연결 단절·취소 시 unknown/reconciling 상태와 영수증으로 처리하고 blind retry하지 않는다.
- WORKSPACE-01: 동시 같은 파일 변경과 stale 결과를 차단/안전 통합하고 최종 결과를 다시 검증한다.
- USAGE-01: 동일 세션의 resume와 이벤트 재전송에서 누적 사용량 중복 집계가 없다.
- PROFILE-01: 같은 프로젝트의 동시 실행에서 Ponytail mode·스킬·정책이 서로 바뀌지 않는다.

- CONTEXT-01: A에서 B로 전환하면 새 세션을 사용하고 A의 대화·REPL·cache가 B의 model 입력/출력에 섞이지 않는다.
- REVOKE-01: 실행 중 자료/도구/provider 권한을 철회하면 다음 호출과 결과 통합이 차단되고 기존 context의 안전한 재사용을 가정하지 않는다.
- RESTORE-01: 오래된 백업 복원 후 삭제 기억·소비된 승인·과거 리스가 활성화되지 않고 외부 효과를 대조한다.
- ACCEPTANCE-01: 사용자 요구 변경은 revision으로 기록되고 worker가 완료 기준을 완화하거나 낡은 증거로 완료할 수 없다.
- INCIDENT-01: 노출 의심 시 중지·격리·범위 보고·자격증명 처리·재개 조건을 확인한다.
- INGEST-01: 미분류 Vault 자료와 금지 경로는 검색·모델 입력에 들어가지 않는다.
- BUDGET-01: 서로 다른 실패·재시도·자식 호출도 목표 전체 상한을 공유하고 무단 자동 연장되지 않는다.
- LOOP-01: 실패/목표 미달에서 가설·최소 변경·영향 검증·진전 판정을 반복하고 통과 시 종료한다.
- LOOP-02: 변화 없는 재시도와 NO_PROGRESS 재개를 차단하고 resume에서도 누적 상한을 유지한다.
- LOOP-04: 변경 전에 필수 검증·안전 종료·복구 예산을 예약하고 부족하면 시작하지 않는다.
- LOOP-03: 유효 증거를 조건부 재사용하되 권한/승인/외부 현재 상태 확인을 생략하지 않는다.
- ECONOMY-01: 모든 모델 작업이 목적과 예산에 연결되고 idle manager/heartbeat 호출이 없다.
- ECONOMY-02: 선택 개선이 완료 목표를 붙잡지 않고 효과 없는 개선을 중지/복구한다.
- ECONOMY-04: 선택 개선은 허용된 상위/개선 예산을 소비하고 완료 목표 재개·상한 초기화·무한 후보 생성으로 우회하지 않는다.
- ECONOMY-03: 조율·검수·실패·유지보수 비용을 포함해 기능을 채택한다.
- RESUME-01: quota 오류와 강제 종료 후 마지막 확정 checkpoint/산출물에서 복구하며 마지막 요약 모델/Stop hook 성공에 의존하지 않는다.
- RESUME-02: checkpoint 후 부분 파일 변경·쓰기 오류·stale export를 감지하고 미검증 결과를 DONE으로 승격하지 않는다.
- HANDOFF-01: Claude→Codex와 Codex→Claude가 같은 목표/완료 기준·잔여 예산·재시도·증거를 이어받는다.
- HANDOFF-02: 이전 writer가 살아 있거나 외부 action이 unknown이면 해당 쓰기/재실행을 차단한다.
- HANDOFF-03: provider·권한·tool/skill 불일치, packet 변조·금지 자료, 소비 승인 재사용을 차단한다.
- RESUME-03: 여러 목표·PAUSED/CANCELLED·NO_PROGRESS·예산 소진은 적절한 선택/대기를 유지하며 자동 초기화하지 않는다.
- ALPHA-01: 프로젝트마다 하나의 지속 Alpha identity/권한 namespace를 유지하고 CLI 교체가 중복 writer/예산을 만들지 않는다.
- ALPHA-02: 전체 Alpha는 허용된 metadata/공용 교훈만 사용하며 프로젝트 간 자료와 메시지 권한을 분리한다.
- LEARN-01: 정제 경험을 다음 업무에서 회상하고 평가된 절차 개선만 버전으로 적용한다.
- LEARN-02: Prime 내부 refine이 staging/고정 평가를 우회하거나 정책·예산·검증기를 바꾸지 못한다.
- FOLDER-01: 프로젝트/작업 생성은 메모리 폴더만 생성하고 원본 프로젝트 clone/worktree/이동을 기본 실행하지 않는다.
- FOLDER-02: Vault에 코드·실행 스킬·키·raw transcript를 쓰지 않고 runtime/기억/실제 프로젝트를 분리한다.
- VALUE-01: 동일 모델/조건의 기본 CLI 대비 비용과 기능을 비교하고 이득 없는 optional 기능을 끈다.
- VALUE-02: 실패·복구·인계·조율·학습·유지보수/입력 overhead를 포함하며 모델 차이 효과와 혼동하지 않는다.
- DRIVER-01: run마다 한 execution_driver만 task 루프를 소유하고 Prime/native CLI 이중 진행을 차단한다.
- RECOVERY-PIN-01: 활성 checkpoint의 복구 자료는 TTL에서 보호하며 pin 삭제/철회·저장 부족을 무시하지 않는다.
- ORIGINAL-01: 반영 전 원본 baseline 변화와 부분 적용을 검출하고 사용자 변경을 무조건 덮어쓰거나 reset하지 않는다.
- CHECKPOINT-COMMIT-01: recovery 객체 내구 저장 전 DB checkpoint를 READY로 확정하지 않고 각 commit 경계의 강제 종료/누락/손상을 처리한다.
- HOLDOUT-01: 후보 생성기가 holdout/정답/상세 trace/평가 기억을 읽지 못하고 최종 평가의 독립성을 확인한다.
- VALUE-CONTRACT-01: 비용 효율/기능 편익 계약을 사전에 정하고 추가 비용과 검증 결과를 그 계약으로 보고한다.
- WAIT-01: 답변/승인 대기 분기는 호출을 멈추고 독립 분기만 허용 예산에서 계속한다.

현재 이 기준을 통과했다고 주장하지 않는다. 설계 검수는 실행 검증을 대체하지 않는다.

## 16. 남은 설치 결정

현재 합의된 운영 규모는 Mac 1인·여러 프로젝트이며 API는 현재 비활성, 미래 선택 가능이다. 구현 시 필요한 나머지 결정은 다음과 같다.

1. 기존 Obsidian Vault의 정확한 경로 또는 전용 Vault 생성 위치.
2. 실제 승인 제공자와 계정, 프로젝트별 외부 전송 범위.
3. 설치 대상 터미널/IDE와 Managed launch 기본 적용 범위.
4. 구독 로그인과 호환되는 격리 환경, 하드웨어·OS 제약.
5. 업무별 기본 실행 시간·재시도·동시성·메모리 예산과 백그라운드 지속 여부.
6. 보존 기간·백업 위치·동기화 제외 영역.

없는 답변을 승인으로 간주하지 않는다. 설치에 필요한 결정은 구체적 후보와 영향이 준비된 시점에만 질문하고, 독립적으로 가능한 작업은 계속한다.

## 17. Ponytail 참고와 자체 저장소 전략

상세 점검은 LUPUS-PONYTAIL-ASSESSMENT.md에 기록한다. 확인한 소스는 commit c982cd411abb53323c4baa1baa3c2f020b8d0b08, plugin 4.10.3이다.

Ponytail은 모델 학습 엔진이나 토큰 압축 저장소가 아니라 구현 선택과 불필요한 복잡성을 줄이는 지침·훅·스킬 패키지다. 프로젝트 내 재사용 → 표준 라이브러리 → 플랫폼 기본 기능 → 설치된 의존성 → 필요한 최소 구현이라는 판단 순서를 참고한다. 보안·오류 처리·접근성·사용자의 필수 요구는 비용 때문에 줄이지 않는다.

코딩 업무에만 기본 lite 후보를 적용하고 full은 평가 후 선택한다. ultra, 삭제 중심 repo audit, 매우 짧은 응답 규칙은 범용 조직 전체에 자동 적용하지 않는다. 이미 주입한 지침은 context hash로 중복을 방지하고 읽기 전용 조사 작업에는 코딩 지침을 불필요하게 주입하지 않는다. host-native 플러그인과 supervisor 지침을 동시에 켜 두지 않는다.

현재 Ponytail runtime은 동일 프로젝트 세션의 mode 공유와 last-write-wins 표시를 사용한다. subagent hook은 agent type 누락·invalid matcher에서 주입으로 fallback하므로 역할 보안 경계가 아니다. supervisor의 run별 고정 지침을 사용하거나 검증된 독립 plugin data profile을 사용해 변경의 상호 간섭을 방지한다. [Runtime source](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/hooks/ponytail-runtime.js), [Subagent source](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/hooks/ponytail-subagent.js).

자체 팀 저장소를 제품 원본으로 만들고 upstream은 버전/commit 고정 adapter로 연결한다. 세 전체 코드베이스를 하나로 합치기보다 정책·상태·UI·adapter·평가를 소유한다. upstream 내부 수정을 해야 하는 명확한 요구가 생기면 작은 패치의 fork를 만들 수 있다. 사용자 GitHub에 fork를 실제 생성하거나 공개하는 작업은 이번 문서 점검에서 실행하지 않았다.

제안 저장소 구조:

~~~text
lupus/
  supervisor/
  launchers/
  adapters/claude/
  adapters/codex/
  adapters/ruflo/
  adapters/prime/
  adapters/agentmemory/
  policies/
  retrieval/
  obsidian/
  evaluations/
  integrations/ponytail/
  upstream-lock.json
  patches/
  THIRD_PARTY_NOTICES.md
~~~

fork 자체는 토큰·품질 개선 기능이 아니다. 추적 가능한 patch set, upstream 보안 업데이트, 기능별 off switch, 라이선스 고지를 관리한다. 전체/프로젝트 Alpha의 인지·학습 harness는 Prime로 두고, Ruflo는 선택 조율, Ponytail은 코딩 구현 지침으로 제한해 지휘권·루프·context의 중복을 줄인다.

## 18. 초기 Astra 검수 이력

gpt-6-astra가 초안과 수정된 세 문서를 독립 검수했다. 초기 1.0 수정본에서 A01~A11 반영을 확인했으며 당시 검토 범위에서 새로 발견한 치명적 설계 누락은 없다는 판정을 받았다. 이후 확장 요구와 실패 경로 검토에서 발견한 추가 보완은 20~25절과 별도 검수 기록에 반영했다. 설계 문서로서 단계적 구현에 착수할 수 있다.

구독 인증과 강한 격리의 양립, 엄격한 입력 통제, 양 CLI 자동 활성화는 구현 시험에서 확인해야 한다. 이 판정은 운영 투입이나 보안·성능 시험 통과를 의미하지 않는다. 상세 지적과 반영 내용은 [LUPUS-ASTRA-REVIEW.md](LUPUS-ASTRA-REVIEW.md), 저장소 분석은 [LUPUS-PONYTAIL-ASSESSMENT.md](LUPUS-PONYTAIL-ASSESSMENT.md)에 기록했다.

## 19. 보고 정책 추가 검수

버전 1.1은 사용자의 Caveman 제안을 반영했다. Astra가 추가 보고 정책을 재검수했고 설계로서 적합하며 현재 채택 범위에서 중대한 누락은 없다고 판단했다. 저장 전 민감정보 배제 권고를 반영했다. CLI 설정이나 기존 스킬 원본은 변경하지 않았다.

## 20. 전체 재검수 보완

이전 검수 이후 새 관점으로 context 전환, 실시간 권한 철회, 백업 복원, 입력 경로의 구현 가능성, 완료 기준 변경, 대기 분기의 논리 일관성을 다시 검토했다. 세부 발견·반영·남은 실행 조건은 [LUPUS-FULL-REVIEW.md](LUPUS-FULL-REVIEW.md)에 기록한다.

## 21. Lupus 고도화 우선순위

| 영역 | 보완 | 확인 지표 |
|---|---|---|
| 업무 루프 | 필수/선택 개선 분리, 진전 기준, NO_PROGRESS | 완료율·무진전 호출·재작업 |
| 배정 | 대표 재사용, 필요할 때만 분업 | 총사용량·조율비·중복 |
| 메모리/RAG | 필요한 검색·선별 저장·증분 색인 | recall·context 비용·권한 일관성 |
| 시험 | 영향 검사·유효 증거 재사용·필요한 최종 회귀 | 결함 누락·재시험 시간 |
| 스킬/통합 | metadata cache·부족한 부분만 개선·선택 adapter | 호환·입력 부하·유지보수 |
| Prime | 실제 손실 trigger·후보 합치기·평가 상한 | 개선비·실측 편익·회귀 |
| 보고 | 짧은 보고·기존 근거 링크·로컬 집계 | 출력·재질문·누락 승인 |

동시 전체 구현보다 최소 보안/구독 가능성을 먼저 확인하고 가장 큰 실제 손실부터 개선한다. 2026-10-05 사용자 요청으로 계획 문서와 근거 자료를 지정한 LUPUS 폴더로 옮기고 문서 파일명·링크를 LUPUS 기준으로 통일했다. 실제 업무 프로젝트·Vault를 이동한 것은 아니다.

## 22. Lupus 1.3 검수 기록

Astra는 비용 원칙·필수/선택 개선·NO_PROGRESS·부모 예산·조건부 증거 재사용·idle 모델 금지의 일관성을 확인했다. 추가 지적인 변경 전 검증/안전 종료/복구 예산 예약과 완료 후 선택 개선의 별도 예산 소유자를 본문에 반영했다. 이 검수는 문서 검토이며 실행 시험은 아니다. 변경 요약은 [LUPUS-REVISION.md](LUPUS-REVISION.md)에 기록했다.


## 23. 기본 Codex/Claude보다 나은지 확인하는 제품 기준

Lupus의 제품 목표는 모델을 교체해 똑똑해졌다고 주장하는 것이 아니라 동일 모델 위의 prompt·context·workflow·기억·복구·환경·통제에서 검증된 비용/기능 이득을 만드는 것이다. 설치된 기능 수나 Alpha 수는 성과가 아니다. 비교에서 이득 없는 optional 기능은 끄고 기본 CLI 경로에 가까운 lean 실행을 사용한다. 필수 안전 장치는 성능 때문에 끄지 않는다.

### 23.1 비교 조건

A는 기본 CLI의 실제 기본 기능(native resume/compact 포함)과 짧은 보고 지침을 사용하는 임시 프로필이다. B는 같은 보안 경계/도구/모델에 Lupus 기능을 끈 대조군이며 환경 통제 비용을 분리한다. C는 Lupus다. 사용자의 현재 설치 설정도 snapshot으로 기록해 실제 사용 환경과의 차이를 밝힌다. 대조군의 기본 기능을 일부러 막거나 불필요하게 장황한 답변을 시켜 유리한 비교를 만들지 않는다.

Claude와 Codex 각각에서 모델/제공자·reasoning 설정·CLI 버전·원본 snapshot·입력·완료 조건·허용 자료/도구·cache/warm 상태를 맞춘다. 모델이 다르면 운영 결과는 비교하되 prompt/workflow만의 인과 효과로 주장하지 않는다. 실패·timeout·재시도·복구·평가·학습 비용을 포함하고 관측 불가 항목은 unknown으로 표시한다. 초기 작은 pilot 후 필요한 범위만 확대한다.

### 23.2 비용과 기능의 채택 기준

| 측면 | 비교 지표 | 채택 조건 |
|---|---|---|
| 비용 효율 | 전체 시도 token/cache/time, 완료당 사용량, 재작업·추가 비용 | 완료 품질/보안 유지 상태에서 실측 개선 |
| 복구 | 강제 종료/quota 후 완료율, 재실행된 완료 작업 수, 유실 범위 | 내구 checkpoint부터 복구, 반복 설명/중복 외부 실행 감소 |
| AI 인계 | 양방향 재개 성공, 인계 입력량/시간, 사용자 재설명 | 허용 자료·동일 목표·잔여 예산으로 continuation, 차단 조건 정확 |
| 조직/학습 | 목표 진전·유사 업무 재작업·개선비·권한 위반 | 경험의 관측된 도움, 상위/프로젝트 경계와 지출 상한 유지 |
| 가시성 | 상태 최신성·다음 행동·추가 사용자 질문 | 기록에서 작업 상태를 파악, 부정확한 완료/승인 표시 없음 |

기본 lean 프로필은 비용이 증가하는 optional context/분업/학습을 피한다. 더 풍부한 기능에 추가 비용이 드는 경우에는 얻는 기능과 incremental 비용/상한을 드러내고 허용된 업무에서만 사용한다. 비용 감소와 기능 증가 중 어느 이득으로 채택했는지 명시한다. 작은 표본의 우연한 차이를 모든 업무에서 우월하다는 증명으로 확대하지 않는다.

전역 운영 비용을 작은 작업 밖에 숨기지 않는다. startup prompt·hook·MCP schema·handoff·memory·background 작업의 전체 관측 비용과 로컬 유지보수 부담을 기록한다. 안전/복구의 필요한 비용과 근거 없는 낭비를 구별한다. 효과가 없는 최적화가 발견되면 rollback하고, 동일 task에 fallback 시 기존 사용량을 그대로 누적한다.

### 23.3 출시 전 복구/인계 실험

먼저 합성 자료로 로컬 fault-injection을 실시한다. 모델 quota 응답, context 압박, checkpoint 전후 강제 종료, 부분 파일 쓰기, 디스크 오류, 살아 있는 writer, 외부 action 직후 연결 단절, 오래된 checkpoint/권한 철회, 인계 target 미로그인/도구 불일치, usage replay를 검증한다. 이후 작은 실제 모델 pilot에서 Claude→Codex 및 Codex→Claude의 다음 task 진행과 결과를 확인한다.

허용된 초기 완료 기준: 확정 checkpoint와 허용 산출물의 복구, 완료 task의 불필요한 재실행 방지, budget/NO_PROGRESS 초기화 0건, 승인·외부 효과의 무단 재실행 0건, handoff 금지자료 0건을 해당 시험에서 확인한다. 이는 시험 관측 기준이며 모든 장애/누출이 불가능하다는 보장이 아니다. 같은 조건의 기본 CLI 대비 회복 시간·사용자 재설명·전체 비용을 함께 측정한다. 실제 시험 전에는 READY나 우월성 검증 완료로 표시하지 않는다.

## 24. Lupus 1.4 보완 문서

사용자의 중단 복구·다른 AI continuation·기본 CLI 대비 가치·전체/프로젝트 Alpha·Prime 기반 학습·메모리 전용 자동 폴더 요구를 반영했다. 작은 handoff packet 예시와 실행 시험은 [LUPUS-RECOVERY.md](LUPUS-RECOVERY.md)에 정리했다. 실제 설치·CLI 설정 변경·원본 프로젝트/기억 Vault 생성·모델 benchmark는 아직 실행하지 않았다.

Astra의 1.4 검수에서 실행 driver 하나 고정, 활성 checkpoint의 실제 복구 자료 보존, 원본 반영 전 사용자 변경 충돌 검사 세 지적을 받았다. 3·6·15에 반영했다. 자동 개선/자료 경계/가치 검증에는 추가 중대한 모순을 발견하지 않았다는 문서 검수 결과이며 런타임 시험 통과는 아니다.


## 25. 근거 기반 반대 검토와 보고 제한

이번 반대 검토는 문서/공개 소스에 근거한 검토다. 확인된 명세 누락/충돌과 미검증 구현 가능성, 단순 우려를 구분했다. 상세는 [LUPUS-ADVERSARIAL-REVIEW.md](LUPUS-ADVERSARIAL-REVIEW.md)에 기록한다. 확인된 세 항목만 보완했고, Prime bridge·구독 인증/격리·원본 동시 쓰기·실측 이득은 구현으로 증명할 사항으로 남긴다.

기능 보고는 설계 제안 / 문서 검수 반영 / 공개 소스 관찰 / 실행 검증 / 비교 실측을 구별한다. 각 검증 주장에는 실행한 방법·입력 범위·버전·결과·근거 참조를 연결한다. 문서 작성/Markdown 검사와 모델의 검수 동의를 runtime 테스트 통과로 보고하지 않는다. source 일부 검토를 전체 security audit으로 보고하지 않는다. 증거가 없는 절감·자동학습 성과·보호 수준·READY 표시는 unknown/미검증이다. 현재 실제 Lupus 구현·모델 benchmark를 실행했다는 증거는 없다.

Astra는 1.5 수정본의 R01~R03이 문서 수준에서 해소되었고 반대 검토 보고가 과장 없이 사실/가정/미검증을 구분한다고 확인했다. 추가 수정 요구는 없었으며 실행 가능성·보안·성능의 통과를 판정한 것은 아니다.

## 26. Lupus 1.6 구현 전 검토와 첫 구현

구현 관점의 검토에서 구현을 막던 누락·모순 9건(I01~I09)을 확인하고 결정했다: Prime 직접 구독 경로 제외, Budget·Reservation 모델과 정산 규칙, 승인 소비와 action intent의 결합, lease와 writer의 원자적 획득, 상태 전이표, DB에 묶인 일회성 handoff, 모델에 운영 API를 주지 않는 권한 경계, 단계 순서, 객체 GC 규칙. 근거·Codex와의 협의·실측 결과는 [LUPUS-IMPLEMENTATION-REVIEW.md](LUPUS-IMPLEMENTATION-REVIEW.md), 코드가 강제하는 규칙은 [lupus/docs/CONTRACT.md](../../lupus/docs/CONTRACT.md)에 있다.

15장의 승인 기준은 전체 제품의 기준이다. 첫 구현의 완료 기준은 CONTRACT의 불변식 10개와 그 시험이며, 15장 기준 중 통과를 주장하는 것은 그 범위에 한한다. AUTO·SEC·MEMORY·IMPROVE·LEARN·ALPHA-02·VALUE 계열은 시험하지 않았다.

## 27. Lupus 1.8 — 경량 실행 경로와 기본 CLI 대비 실측

사용자 요구(단순 작업에서도 기본 CLI보다 이득)를 반영해 worker 호출을 경량화했다: 작업에 필요 없는 설정을 싣지 않고, 검증기가 보는 파일을 prompt에 동봉하고, 검증을 supervisor가 맡는 대신 worker의 자체 확인을 생략시키며, 교훈 요청은 실패 뒤에만 한다. 이는 1.1의 "필요 없는 호출·입력을 만들지 않는다"와 11.1의 context 예산을 구현한 것이다.

같은 작업·같은 검증기로 사용자의 평소 CLI, 설정을 끈 CLI, Lupus를 비교한 결과 이 Mac에서는 단순 작업 3종과 4단계 업무 모두에서 Lupus가 토큰과 시간을 덜 썼고 검증 통과는 같았다. 23.2의 비용 효율 계약은 이 범위에서 충족했다. 수치·조건·Lupus가 더 쓴 구간·한계는 [LUPUS-IMPLEMENTATION-REVIEW.md](LUPUS-IMPLEMENTATION-REVIEW.md) 1.8절에 있다. 반복 수가 작아 일반적인 우월성의 증명은 아니다.

