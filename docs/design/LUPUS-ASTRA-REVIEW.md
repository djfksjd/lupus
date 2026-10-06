# Astra 독립 설계 검수

검수일: 2026-10-04. 검수 모델: GPT-6 Astra. 사용자가 명시적으로 요청한 독립 검수다.

대상: LUPUS-PLAN.md 0.9 초안과 공개 upstream 문서·소스. 이 문서는 Astra가 반환한 지적과 작성자의 반영 결과를 정리한 것이다. Astra는 파일을 수정하거나 실행 시험하지 않았다.

## 판정

설계 방향은 타당하며 보완 후 단계적 구현에 적합하다. 현재 운영 투입은 미검증이다. 단일 supervisor, 운영 DB·경험 메모리·지식 원본 분리, 구독 우선·미래 API 확장, exact approval, fencing token, 단계적 도입은 적절하다.

가장 중요한 미해결 실행 조건은 구독 인증과 강한 격리의 양립이다. 문서에서 보완했다고 인증·권한·격리가 실제 검증된 것은 아니다.

## 지적과 반영

| ID | 중요도 | Astra 지적 | 계획에 반영한 내용 |
|---|---|---|---|
| A01 | 높음 | 구독 headless 인증·설정 격리 계약 부족 | Claude --bare를 구독에 사용하지 않음, curated staging과 auth/billing 확인 |
| A02 | 높음 | 업무 서비스 키와 CLI OAuth 보호 수준 혼동 | broker 키와 native auth 분리, 인증 자료 접근 시험 전 엄격 보호 수준 미지원 |
| A03 | 높음 | 외부 효과가 불명확한 상태 모델 부족 | EXECUTION_UNKNOWN/RECONCILING, intent·receipt·실제 결과, blind retry 금지 |
| A04 | 높음 | Agent Memory 원시 훅이 자료 정책 우회 가능 | raw capture 경로 목록화·통제, staged/정제 이벤트 하나만 사용 |
| A05 | 높음 | task lease로 같은 파일 충돌을 막지 못함 | workspace 단일 writer 또는 task worktree·순차 통합·최종 검증 |
| A06 | 중간 | cross-CLI 재개 의미 모호 | 공통 체크포인트로 새 run, native transcript resume은 host 내부 기능 |
| A07 | 중간 | resume 누적 사용량 중복 집계 가능 | scope/session/sequence/watermark/source, delta 정산·재전송 dedupe |
| A08 | 중간 | 서비스 계약의 인증/권한 필드 부족 | principal·server binding·policy·idempotency·deadline·revision/fencing |
| A09 | 중간 | 파생 기억의 자료 등급·폐기 범위 부족 | embeddings/FTS/snippets/replay/cache도 상속·삭제·철회 전파 |
| A10 | 낮음 | 문서 언어 오류·근거 없는 절감 목표 | 한국어 정리, 사전 절감 수치 대신 완료 업무 비교 평가 |
| A11 | 높음 | native CLI 직접 입력까지 무유출 보장 불가 | staged/broker/관리 입력으로 보장 범위 한정, local input gate는 시험 대상 |

## 구독 CLI의 필수 확인 사항

현재 Claude 공식 문서에서 bare는 OAuth/Keychain을 읽지 않으며 일반 비대화형 실행은 프로젝트 hooks/MCP를 로드할 수 있다. 따라서 설정 발견을 제거하는 옵션과 구독 로그인을 함께 사용 가능하다고 가정하지 않는다. 상속된 API/provider 설정도 검사한다. resume의 누적 사용량과 취소 후 unfinished turn을 adapter가 처리해야 한다. [Headless execution](https://code.claude.com/docs/en/headless), [Authentication](https://code.claude.com/docs/en/authentication).

CLI가 로그인 자격증명에 접근하는 것과 도구가 같은 자료를 읽지 못하는 것은 별도 문제다. 컨테이너 전용 home만으로 해결되었다고 할 수 없다. 지원되는 인증/실행 경계의 시험 증거가 없으면 해당 보호 수준을 활성화하지 않는다.

## Ponytail 검수

선택적인 코딩 구현 지침으로는 평가 가치가 있으나 토큰 압축·조직 운영 엔진으로 취급하지 않는다. lite부터 평가하고 supervisor·조사·검수 업무에 전역 적용하지 않는다. 사용자 요구·보안·완료 조건을 줄이는 수단으로 사용하지 않는다.

현재 runtime의 프로젝트 공유 mode와 subagent fallback 주입은 고정된 동시 run profile과 충돌할 수 있다. supervisor-owned run별 지침 또는 검증된 독립 profile을 사용한다. [Ponytail runtime](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/hooks/ponytail-runtime.js), [Subagent hook](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/hooks/ponytail-subagent.js).

공개 benchmark는 특정 모델·저장소의 제한된 실험이며 기능 업무의 실제 서버/브라우저 동작 검증과 timeout 집계에 한계가 있다. 보고 수치를 검증된 완료 업무당 절감률로 가져오지 않는다. 상세 수치와 조건은 LUPUS-PONYTAIL-ASSESSMENT.md에 정리했다. [Benchmark](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/benchmarks/results/2026-06-18-agentic.md).

## 세 upstream fork에 대한 판단

모두 깊게 fork하는 초기 기본안은 권하지 않는다. 한 사람이 보안 업데이트·hooks/API 변경·테스트·자체 패치 충돌을 유지해야 한다. 자체 supervisor 저장소와 고정 버전 adapter가 초기 권고다.

- Ruflo: 공개 인터페이스로 감독 dispatch·취소·이벤트를 구현할 수 없을 때 최소 fork 검토.
- Prime: 실행 제한·결과 계약을 외부에서 구현할 수 없고 구독 호환이 증명될 때 검토.
- Ponytail: 필요한 run별 mode 분리가 upstream에서 해결되지 않고 효과가 확인될 때 작은 패치 검토.

fork마다 변경 이유·기준 commit·최소 patch·호환 시험·업데이트와 제거 경로를 기록한다. 실제 fork나 설치는 이번 검수에서 하지 않았다.

## 추가 실행 시험

| 시험 | 확인 대상 |
|---|---|
| AUTH-01 | 실제 구독 경로와 상속 API/provider 차단 |
| AUTH-02 | 도구·shell·MCP·hooks의 CLI 인증 자료 접근 |
| ACTION-01 | 외부 실행 직후 연결 단절·중복 방지·불명확 상태 |
| WORKSPACE-01 | 같은 파일 동시 변경·stale 결과·최종 통합 |
| USAGE-01 | resume 누적값·이벤트 재전송 중복 방지 |
| PROFILE-01 | 같은 프로젝트 동시 run의 지침·mode 상호 간섭 |

현재 이 시험들은 설계된 승인 기준이며 통과 기록은 없다. 실행 검증은 구현 단계에서 수행한다.

## 검수 이력

1. Astra가 0.9 초안을 검토하고 위 지적을 반환했다.
2. 작성자가 해당 지적을 계획과 Ponytail 점검에 반영했다.
3. Astra가 수정된 세 문서를 재검수했다. 이전 A01~A11 반영을 확인했으며 새로 발견한 치명적 설계 누락은 없다고 판단했다.

## 최종 재검수 판정

설계 문서로서 적합하며 단계적 구현에 착수할 수 있다. Ponytail의 코딩 업무 한정 적용과 자체 supervisor·고정 버전 adapter 우선 전략도 타당하다는 검수 결과를 받았다.

구독 인증과 강한 격리의 양립, 엄격한 입력 통제, 양 CLI 자동 활성화는 구현 시험에서 확인해야 한다. Astra는 파일 수정이나 실행 시험을 수행하지 않았으며, 본 결과는 운영 투입 승인이나 토큰 절감 실측 결과가 아니다.

## 버전 1.1 Caveman 추가 검수

Astra가 Caveman 점검과 계획 11.1.1·19를 재검수했다. 간결한 보고에서도 검증·미검증·위험·승인·변경 이유를 보존하고 상세 요청을 존중하며 전체 비용과 재작업을 평가하므로 설계로서 적합하다고 판단했다. 권고한 모델 전송 및 원문 SQLite·로그·백업 저장 전 민감정보 배제를 두 문서에 반영했다. 프록시 설치나 실행 시험은 수행하지 않았다.

## 전체 재검수: 버전 1.2

사용자가 누락과 논리 모순을 다시 검토하도록 요청해 전체를 새 관점으로 검수했다. 이전 정적/정상 실행 중심 검토에서 놓친 프로젝트 context 전환, 실시간 권한 철회, 백업 복원, 완료 기준 변경을 발견했다. 분기 대기·사전 가능성 시험·평가 분모·범용 검증·Caveman 승인 문구도 보완했다. 세부 발견과 반영은 [LUPUS-FULL-REVIEW.md](LUPUS-FULL-REVIEW.md)에 기록했다. 이전 버전의 적합 판정은 이 새 보완 필요성을 없애는 의미가 아니다.

Astra가 1.2 수정본에서 F01~F13 반영을 확인했다. 독립 삭제/철회 기록 소실 시 검색·context·export·Generated 재생성도 차단하라는 마지막 권고를 반영했다. 보완을 전제로 설계 수준의 중대한 잔여 모순은 발견하지 않았으며 P0.5 실행 가능성 시험에 적합하다고 판정했다.

## Lupus 1.3 비용 원칙·실행 루프 검수

Astra가 새 원칙과 반복 흐름의 일관성을 확인했다. 변경 전에 검증/안전 종료/복구 예산을 확보하고 선택 개선 예산 소유자를 명시하라는 두 지적을 본 계획에 반영했다. 자세한 반영은 [LUPUS-REVISION.md](LUPUS-REVISION.md)에 기록했다. 실행 시험은 하지 않았다.

## Lupus 1.4 조직·복구·가치 검수

Astra가 전체/프로젝트 Alpha·Prime 기반 학습·메모리 전용 폴더·양방향 인계·기본 CLI 비교를 검토했다. 단일 execution_driver, 활성 checkpoint의 실제 recovery 자료 pin, 사용자 원본 변경의 baseline 충돌 검사를 보완하도록 지적했고 본 계획에 반영했다. 실행 시험은 수행하지 않았다.

## Lupus 1.5 엄격한 반대 검토

Astra가 실패 경로 중심으로 명세 누락 2건과 채택 기준 충돌 1건(R01~R03)을 제시했다. 작성자는 객체→DB commit, holdout 읽기/feedback 격리, 사전 채택 계약을 반영했다. Astra가 수정본과 [반대 검토 보고](LUPUS-ADVERSARIAL-REVIEW.md)를 확인해 문서 수준에서 해소되었고 보고에 과장된 판정이 없다고 판단했다. 추가 수정 요구는 없었다. runtime·보안·Prime 연결·성능 실행 시험은 하지 않았다.
