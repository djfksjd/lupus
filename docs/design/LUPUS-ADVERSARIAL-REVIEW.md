# Lupus 1.5 엄격한 반대 검토

검토일: 2026-10-04. 검수 대상은 전체 설계 1.4와 LUPUS-RECOVERY.md이며 수정본은 1.5다. 작성자의 근거 대조와 GPT-6 Astra의 독립 실패 경로 검토를 결합했다. Astra는 파일 변경·설치·모델 benchmark를 수행하지 않았다. 작성자는 문서/공개 소스만 읽고 문서를 수정했다.

## 현재 판정

운영 투입 승인이나 비용/학습 성과 입증 단계가 아니다. 확인된 명세 누락 2건과 문구 충돌 1건을 수정할 수 있는 설계다. 아래 수정은 문서상의 실패 경로를 줄이며, 실제 강제 장치의 구현/시험을 대신하지 않는다. Prime 기반 구조와 구독 CLI의 실제 연결성은 미검증이다.

## 확인된 문제와 최소 수정

| ID | 분류/중요도 | 1.4의 근거 | 실패 경로 | 1.5 보완 |
|---|---|---|---|---|
| R01 | 명세 누락 / 높음 | §6.6에 DB transaction/WAL와 파일 atomic rename이 각각 있었지만 둘의 commit 순서는 없었음 | DB checkpoint를 확정한 뒤 필요한 복구 객체 저장이 유실되면 pin/hash만 남고 내용 복원 불가 | 객체 내구 저장/hash 검증 → 참조/pin/checkpoint DB commit. restart 대조·orphan 정리·누락 시 BLOCKED·객체 포함 백업·경계별 종료 시험 |
| R02 | 명세 누락 / 중간 | §12.1은 후보의 holdout/기준 변경을 금지했지만 읽기와 feedback/기억 경로를 명시하지 않음 | 정답을 읽고 맞춘 지침은 파일을 수정하지 않고 평가를 통과할 수 있음 | 후보 생성/평가 권한 분리, private 정답/trace 제외, feedback·질의 상한, 튜닝에 안 쓴 최종 사례, 오염 근거 폐기 |
| R03 | 문구 충돌 / 중간 | §11.3은 사용량/시간/재작업 개선을 채택 조건으로, §23.2는 추가 비용 안의 기능 편익도 허용 | 같은 기능을 한 절에서는 채택하고 다른 절에서는 거부할 수 있음 | 비용 효율 또는 사전 허용 비용 안의 기능 편익으로 통일. 계약을 pilot 전에 정하고 비용 증가를 절감으로 보고하지 않음 |

R01~R03은 문서로 확인한 실패 가능성이다. 실제 데이터 유실·정답 유출·과금 낭비가 발생했다는 보고가 아니다. 상세 수정은 [전체 설계](LUPUS-PLAN.md) 6.6·11.3·12.1과 [복구 계약](LUPUS-RECOVERY.md)에 반영했다.

## 미검증 가정: 문제가 이미 발생했다고 단정하지 않음

| 항목 | 확인해야 할 근거 | 실패 시 처리 |
|---|---|---|
| Prime↔native CLI bridge | 실제 지원 호출 경로·구독 인증·단일 driver·하위 호출/취소/usage 통제의 P0.5 기록 | Prime 기반 완료 표시 금지, 제한 경로/대기. 무단 API 전환 금지 |
| 인증/입력/실행 격리 | source/code/hook/MCP/child가 scope 밖 자료와 native auth에 접근하지 못하는 해당 환경 시험 | 보호 미지원 표시, 보호 업무 실행 금지 |
| 원본 동시 반영 | 사용자 IDE 경합·부분 적용에서 baseline/journal/publish 경계 시험 | 덮어쓰기 제한, 충돌 기록·정제된 수동/재계획 경로 |
| 내구 복구·AI 인계 | 실제 저장 순서·강제 종료·old writer·unknown action·양방향 인계 시험 | READY 금지, 확정 상태/차단 사유 표시 |
| 자동학습 편익 | 고정/분리 평가·실사용 회귀·투입 개선비를 포함한 결과 | 자동 승격 비활성 또는 rollback |
| 기본 CLI 대비 우월성 | 같은 모델/조건의 기본 native 기능 포함 비교와 전체 비용/기능 지표 | 우월성 주장 금지, 이득 없는 optional 기능 비활성 |

현재 위 시험의 Lupus runtime 통과 기록은 없다. 이 검토에서 새 runtime 테스트나 모델 비교를 실행하지 않았다. 기존 Markdown 읽기·링크 검사는 문서 무결성 확인이다.

## 공개 소스에서 관찰한 사실과 그 한계

Prime 일부 소스를 commit bc57309434ab56da5960e350a1b1f2744ba29ac6으로 고정해 evidence/prime에 저장했다. 전체 clone·보안 감사·설치가 아니며 저장소 현재 main이 바뀌어도 점검 대상은 이 commit이다.

- README는 supplemental harness 개선과 별도 외부 sandbox 필요성을 설명한다. [README](https://github.com/PrimeIntellect-ai/prime-agent/blob/bc57309434ab56da5960e350a1b1f2744ba29ac6/README.md).
- 검토한 provider 코드에는 자체 Codex endpoint 요청 구성과 Anthropic OAuth 흐름이 있다. 이것은 Prime 경로의 관찰이며 기존 codex/claude 바이너리를 실행하는 Lupus bridge가 검증됐다는 증거가 아니다. 로그인 성공·과금·현재 제공자 지원 정책·안전한 auth 분리는 이번 소스 읽기에서 시험하지 않았다. [Codex request](https://github.com/PrimeIntellect-ai/prime-agent/blob/bc57309434ab56da5960e350a1b1f2744ba29ac6/crates/pa-ai/src/providers/openai_codex_responses/request.rs), [Anthropic OAuth](https://github.com/PrimeIntellect-ai/prime-agent/blob/bc57309434ab56da5960e350a1b1f2744ba29ac6/crates/pa-ai/src/oauth/anthropic.rs).
- refinement executor에는 후보를 계획하고 harness state에 적용하는 함수가 있다. 이 사실은 Lupus의 holdout/예산/자료 정책 검증기가 이미 구현됐음을 뜻하지 않는다. 추가 supervisor adapter의 staging/승격 경계가 필요하다. [Executor](https://github.com/PrimeIntellect-ai/prime-agent/blob/bc57309434ab56da5960e350a1b1f2744ba29ac6/crates/pa-core/src/refinement/executor.rs).

단순히 “subscription 지원” 또는 “self-improving”이라고 적혀 있다는 이유로 Lupus의 계약 충족을 확정하지 않는다. 소스 관찰에서 지원 불가·약관 위반·누출 발생을 추정해 단정하지도 않는다.

## 결함으로 판정하지 않은 우려

- Alpha 계층은 lazy 호출·대표 재사용·공유 예산이 명시되어 있어 그 존재만으로 상시 비용 낭비라고 단정하지 않는다.
- 별도 runtime의 제한 복구 자료는 기억 Vault의 clone/code 생성과 다르므로 사용자의 메모리 전용 자동 폴더 요구에 직접 충돌하지 않는다.
- 기본 CLI의 native resume/compact와 간결한 답변을 baseline에 포함하므로 의도적으로 불리하게 만든 대조군이라는 근거는 없다.
- 복구 객체가 존재한다는 것만으로 모델에 노출된다고 볼 근거는 없다. 자료 권한/packet 필터의 실제 구현 검증은 여전히 필요하다.
- 아직 실행하지 않은 테스트는 실패로도 성공으로도 계산하지 않는다.

## 근거 있는 다음 단계

기능을 더 늘리지 않고 기존 P0.5를 먼저 실행하는 것이 현재 근거에 맞는 순서다. 최소 판정 대상은 Prime/native driver 연결, 구독 경로·보호 입력, 복구 commit 경계, 허용 파일 쓰기, 최소 양방향 인계다. 실패하면 원인/범위만 보고하고 해당 보호/Prime 기능을 활성화하지 않는다. 실제로 연결성이 확인된 뒤 작은 업무 pilot으로 비용 또는 기능 편익을 측정한다.

## 수정본 확인

Astra가 R01~R03 수정본과 본 보고서를 확인했다. 세 항목은 설계 문서 수준에서 해소되었으며, 보고서가 설계 실패 가능성·미검증·실행 결과를 구분하고 검토 범위에서 과장된 판정이 없다고 확인했다. 추가 수정 요구는 없었다. 이 판정은 runtime·보안 강제·Prime 연결·성능 실행 검증이 아니다.
