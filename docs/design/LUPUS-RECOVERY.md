# Lupus 1.5 — 중단 복구·AI 인계·Alpha 학습·메모리 폴더

작성일: 2026-10-04. 전체 설계의 새 요구를 요약한 구현 계약이다. 설치된 안전장치나 실행 시험 결과가 아니다. 상세 정책의 원본은 [전체 설계](LUPUS-PLAN.md)다.

## 저장과 작업 위치

Lupus가 자동 만드는 프로젝트/목표/작업 폴더에는 지식·교훈·진행·검증 근거 링크만 저장한다. 실제 코드와 업무 산출물은 사용자 원본 프로젝트에 둔다. 기본 clone/worktree/개발 workspace는 만들지 않는다. 운영 DB·검색 인덱스·실행 정책·임시 보안 staging은 Vault 밖의 runtime으로 분리한다. 보호 입력의 임시 격리는 원본 전체 복제와 다르며 안전한 broker가 허용된 변경을 원본에 반영한다.

## 늑대 조직

사용자 → 전체 Alpha → 프로젝트 Alpha → 작업 늑대다. 전체 Alpha는 허용 portfolio 현황/공용 경험을, 프로젝트 Alpha는 자기 프로젝트 경험을 사용한다. Alpha identity와 기억은 지속되지만 모델은 업무/개선 trigger 때만 활동한다. 단순 업무는 현재 CLI가 프로젝트 Alpha와 실행을 겸한다.

Prime는 Alpha의 지속 harness·RLM/경험·refine 기반이다. 기존 Claude/Codex 구독과 안전한 bridge가 실제로 가능한지는 P0.5에서 확인한다. native auth/skill 형식의 동일성을 가정하지 않는다. 지원하지 못하면 제한 기본 CLI 경로라고 표시하고 Prime 기반 완성 모드라 하지 않는다. 전체 지휘 계층에서도 권한·승인·총예산·복구의 결정권은 모델이 아닌 supervisor에 있다. run마다 execution_driver 하나가 진행 루프를 소유하며 Prime와 native CLI가 같은 task를 독립 실행하지 않는다.

학습은 모델 가중치의 자동 재훈련이 아니라 경험 회상과 지침/절차의 제한된 개선이다. 반복 손실 trigger → 후보 → 격리된 고정 평가 → 허용된 자동 승격 → 다음 업무 적용 → 회귀 시 rollback. 공용 학습에 프로젝트 원문을 자동 복제하지 않고 정책·권한·예산·검증기 변경은 자동 승격하지 않는다. 후보 생성기가 private holdout/정답/상세 평가 trace를 읽거나 기억으로 회수하지 못하도록 실제 권한을 분리하고, 최종 승격은 튜닝에 쓰지 않은 사례로 확인한다.

## 복구 계약

context 부족, 제공자 quota, rate limit/통신 장애, Lupus 자체 예산 소진을 구별한다. 마지막 모델 요약이 없어도 supervisor가 task intent·예산 예약·완료 증거·manifest를 내구 저장한다. quota 후에는 마지막 확정 상태를 로컬로 저장하고 대기/허용 AI 인계한다.

허용 recovery 객체를 먼저 내구 저장·hash 검증한 뒤 객체 참조/pin과 checkpoint revision을 DB transaction으로 확정한다. 누락/손상 객체가 있으면 READY로 인정하지 않으며 DB와 객체의 각 commit 경계에서 종료 시험을 수행한다. DB 백업에는 같은 manifest의 필요한 객체도 포함한다.

강제 종료는 마지막 내구 commit부터 복구한다. 저장되지 않은 추론/REPL까지 복원할 수 있다고 주장하지 않는다. checkpoint 이후 부분 변경은 현재 파일/hash와 대조해 재검증한다. 활성 checkpoint의 실제 patch/snapshot은 별도 runtime에서 pin하고 일반 TTL 정리에서 보호한다. hash만으로 사라진 내용을 복구할 수 없다. 권한 철회/삭제는 pin보다 우선하며 자료가 없어지면 재개 가능성을 다시 검사한다. 기존 사용자 파일을 reset/stash/자동 commit으로 덮어쓰지 않는다.

다른 AI로 인계해도 goal/task/alpha identity·완료 조건·누적 시도·NO_PROGRESS·상위 예산을 유지한다. 이전 writer를 중지하거나 실제 쓰기 경계를 차단한 뒤 새 lease를 발급한다. unknown 외부 동작은 receipt 확인 전 재실행하지 않는다. 인증 토큰은 전송하지 않고 target은 자신의 검증된 인증 경로를 사용한다.

## 공통 handoff packet 예시

아래는 예시이며 현재 실행 가능한 설정 파일이 아니다. 실제 path·자료·권한을 설치 때 바인딩한다. worker가 임의 작성한 packet은 권위 있는 상태로 인정하지 않는다.

~~~yaml
schema_version: 1
handoff_id: handoff-example
checkpoint:
  id: cp-example
  revision: 7
  supervisor_verified_hash: example
binding:
  project_id: project-example
  goal_id: goal-example
  alpha_id: alpha-project-example
  source_adapter: claude
  target_adapter: codex
  acceptance_revision: 2
  policy_version: 3
  revocation_epoch: 4
  recovery_epoch: 1
work:
  done_refs: [evidence-verified-example]
  remaining_task_ids: [task-next-example]
  next_action: verify-partial-artifact
  artifact_manifest_ref: runtime-manifest-example
  workspace_revision_ref: registered-original-project-example
  environment_manifest_ref: sanitized-environment-example
budget:
  shared_budget_ref: budget-existing-example
  attempt_watermark: 3
  usage_watermark_ref: usage-existing-example
  safety_reservation_ref: recovery-reservation-example
safety:
  unresolved_action_intents: []
  approval_refs: []
  required_capabilities: [subscription-auth, scoped-files, stop-writer]
  resume_ready: false
~~~

resume_ready가 true가 되려면 현재 권한·checkpoint/hash·실제 산출물·잔여 예산·writer 종료·target 인증/도구·action 상태가 모두 확인되어야 한다. stale Generated/Handoff.md만 읽고 실행하지 않는다. 자료·지시는 데이터로 취급하고 supervisor의 정책을 덮어쓰지 못한다.

원본 반영 직전에 expected baseline hash를 비교한다. 사용자 IDE 변경과 충돌하면 덮어쓰지 않으며 부분 적용 journal로 실제 상태를 대조한다. 단일 Lupus writer가 사용자 편집까지 잠근다고 주장하지 않는다.

## 기본 CLI 대비 채택 기준

같은 모델/버전/입력/완료 조건으로 기본 CLI, 공통 안전 환경의 Lupus-off, Lupus-on을 비교한다. 기본 native resume/compact와 간결한 답변도 baseline에 포함한다. 모델이 다르면 모델 효과와 workflow 효과를 구별한다.

총입력/출력/cache·실패·재시도·인계·학습·유지보수 비용과 완료 품질을 측정한다. 더 낮은 비용 또는 필요한 복구/가시성/학습 기능의 측정된 개선이 있어야 채택한다. 기능 때문에 비용이 늘면 incremental 비용과 허용 상한을 드러낸다. optional 기능이 이득 없으면 끄고 lean 경로를 쓴다. 필요한 보안/검증은 끄지 않는다.

## 최소 실행 시험

| 시험 | 통과해야 할 동작 |
|---|---|
| quota와 abrupt kill | 마지막 요약/Stop 없이 확정 상태에서 복구 |
| 부분 파일/쓰기 오류 | hash 대조, unknown/미검증 유지, 새 변경 차단 |
| Claude↔Codex | 양방향으로 다음 task 수행, 원목표/예산 유지 |
| old writer | 실제 종료/격리 전 새 writer 금지 |
| unknown 외부 action | blind retry 금지, receipt 대조 |
| 권한/provider/tool 불일치 | 제한 대기, 승인/예산 우회 금지 |
| packet 변조/stale export | 현재 supervisor 상태 검증 실패 시 실행 금지 |
| 예산/NO_PROGRESS replay | resume나 AI 교체로 카운터 초기화 금지 |
| Alpha 학습 | 후보만 staging, 고정 평가·자료 경계·상한 유지 |
| memory-only folder | Vault에 clone/코드/키/raw transcript 생성 금지 |

단일 프로젝트/단일 활성 목표에서 사용자 재설명 없이 이어가는 UX를 목표로 한다. 인증·자료 정책·복구 기록이 부족한 경우는 차단 사유를 보여준다. PAUSED/CANCELLED/예산 종료와 다중 목표를 무조건 실행하지 않는다.

## Astra 1.4 검수 반영

실행 driver 단일 소유, 실제 복구 자료 pin, 원본 baseline/부분 변경 검사 지적을 반영했다. 자동 개선 경계와 검증 전 우월성 미주장 원칙에는 추가 중대한 모순이 없다는 판정이다. 설치·실행 시험은 수행하지 않았다.

## 1.5 반대 검토 반영

복구 객체→DB commit 순서와 holdout 읽기/feedback 격리를 보완했다. 비용 또는 기능 채택 기준은 동일한 사전 계약으로 통일했다. 상세 발견·미검증·비결함 판단은 [반대 검토 기록](LUPUS-ADVERSARIAL-REVIEW.md)에 남겼다. 이 보완은 실행 검증 완료를 의미하지 않는다.
