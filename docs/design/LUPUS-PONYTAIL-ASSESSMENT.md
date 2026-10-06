# Ponytail 점검과 AI 팀 통합 판단

작성일: 2026-10-04. 문서·소스 점검 결과이며 모델 benchmark를 실행하거나 플러그인을 설치하지 않았다.

## 확인 대상

- 저장소: [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)
- 확인 commit: c982cd411abb53323c4baa1baa3c2f020b8d0b08
- manifest 버전: 4.10.3
- 공개 소스 일부는 evidence/ponytail에 고정 commit으로 저장했다. 이 폴더는 전체 clone이나 fork가 아니다.

## 결론

코딩 업무의 불필요한 구현·의존성·추상화를 줄이는 접근은 참고 가치가 있다. 메모리 압축이나 기반 모델 학습 기술로 이해하면 안 된다. 전체 조직의 토큰 절감이나 보안 보장으로 일반화할 근거는 부족하다.

세 저장소를 포크할 수는 있지만, 자체 조직의 제품 원본을 별도 저장소에 두고 버전 고정 어댑터로 연결하는 방식을 우선 권장한다. 꼭 필요한 upstream 내부 변경만 작은 fork 패치로 유지한다.

## 성능 주장 점검

작성자가 공개한 2026-06-18 실험은 Haiku 4.5, Claude Code 2.1.177, FastAPI/React template의 기능 업무 12개, 각 조건 n=4다. 보고된 평균은 코드 추가량 약 54%, 토큰 약 22%, 추정 비용 약 20%, 시간 약 27% 감소다. 날짜 선택기처럼 플랫폼 기본 기능을 쓸 수 있는 업무에서 절감이 크고 이미 간단한 CRUD는 차이가 작다.

중요한 한계: 기능 업무의 서버·브라우저를 실행해 완성 동작을 검증하지 않았으며, 일부 timeout 결과는 LOC에 포함되고 시간·비용에서는 빠졌다. 안전성 결과는 제한된 공격 테스트에서 관찰한 통과율이다. 코드량 감소를 완료 품질이나 모든 보안의 증명으로 취급하지 않는다. 출처: [공개 benchmark](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/benchmarks/results/2026-06-18-agentic.md).

README는 추론 모델에서 추가 사고 때문에 비용이 반대로 증가할 수 있는 사례도 언급한다. 따라서 현재 Claude·Codex 모델에서 재평가해야 한다. [README](https://github.com/DietrichGebert/ponytail).

## 소스에서 확인한 자동 적용 방식

Claude/Codex manifest가 SessionStart·SubagentStart·UserPromptSubmit hooks를 연결한다. 시작 시 선택 모드의 지침을 주입하고, 하위 에이전트에도 적용할 수 있으며, prompt 이벤트는 모드 변경을 추적한다. 기본은 full, 설정/환경변수로 lite/full/ultra/off를 선택한다.

단순 MCP 연결은 항상 활성화를 보장하지 않는다. ponytail-mcp README도 prompts가 사용자 호출이며 모든 호스트에 매 턴 자동 주입하는 공용 MCP 규약은 없다고 설명한다. [Hook contract](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/hooks/claude-codex-hooks.json), [MCP 설명](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/ponytail-mcp/README.md).

## 채택할 부분과 제한할 부분

| 부분 | 판단 | 적용 방식 |
|---|---|---|
| 실제 요구와 관련 코드 이해 | 채택 | 작은 diff보다 동작과 근거 우선 |
| 기존 코드·표준 라이브러리·플랫폼 기능 재사용 | 채택 | 코딩 업무의 구현 선택 순서 |
| 불필요한 의존성·추상화 방지 | 채택 | 처음부터 모든 시스템을 합치지 않음 |
| 안전·데이터 손실 방지·접근성 유지 | 채택 | 독립 검증과 정책을 그대로 유지 |
| lifecycle activation | 참고 | 공용 context builder에 통합, 중복 hook/규칙 금지 |
| full/ultra 기본 적용 | 제한 | lite부터 우리 평가에서 비교 |
| 응답을 극도로 짧게 제한 | 제한 | 설계 보고·조사·승인 설명에는 적용하지 않음 |
| 한 개 검사만으로 충분하다는 운영 | 제한 | 위험도와 완료 조건에 필요한 검증 유지 |
| 삭제 중심 전체 repo audit | 선택 | 변경 근거와 회귀 검증이 준비된 작업에만 사용 |

지침 세부 내용은 [instruction builder](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/hooks/ponytail-instructions.js)에서 확인했다. 우리 정책이 상위이며 단순화 지침은 승인·보안·필수 요구를 대체하지 않는다.

## 보안과 추가 비용 점검

- 확인한 activation/config 코드는 로컬 상태와 설정을 읽거나 기록하고 지침을 출력한다. 이 범위에서 네트워크 전송 구현을 발견하지 않았지만 전체 저장소의 무유출을 검증한 것은 아니다.
- runtime은 동일 프로젝트 세션의 mode 공유와 last-write-wins 표시를 사용한다. agent type 누락·invalid matcher에서는 subagent 지침 주입으로 fallback한다. 같은 프로젝트 동시 작업과 역할 제한에 그대로 쓰지 않고 supervisor가 run별 고정 지침을 제공한다. [Runtime](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/hooks/ponytail-runtime.js), [Subagent hook](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/hooks/ponytail-subagent.js).
- plugin hooks는 실행 코드이므로 격리·권한·업데이트 검토 대상으로 둔다. 설치되었다는 사실은 안전성의 증거가 아니다.
- benchmark의 judge.py는 Anthropic API 키를 읽고 코드 내용을 API로 전달하는 별도 평가 경로다. 현재 구독 전용 정책에서는 그대로 실행하지 않는다. 필요한 검수는 구독 CLI adapter와 정제된 입력으로 구성한다. [judge.py](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/benchmarks/agentic/judge.py).
- MIT 고지와 저작권 고지를 복사·변경한 배포물에 보존하고, 각 upstream 및 의존성 고지도 별도로 추적한다. [Ponytail LICENSE](https://github.com/DietrichGebert/ponytail/blob/c982cd411abb53323c4baa1baa3c2f020b8d0b08/LICENSE).

## 세 저장소의 자체 팀 구성

| upstream | 우리 팀에서 역할 | 초기 전략 |
|---|---|---|
| Ruflo | 복잡 업무의 배정·조율 | 버전 고정 adapter, supervisor 권한 아래 사용 |
| Prime Agent | 전체/프로젝트 Alpha의 학습 가능한 harness 기반 | 1.4 요구 반영: 구독 bridge·격리·감독 계약을 검증한 후 연결 |
| Ponytail | 코딩 구현의 경제성 지침 | 필요한 lite 지침/adapter만 선택해 중복 주입 방지 |
| Agent Memory | 경험 회상 | 기존 설치 재사용, 원시 전역 capture 통제 |
| Obsidian | 지식·진행 현황 | 수동 원본과 Generated 영역 분리 |

세 fork를 별도 유지하는 경우에도 하나의 상위 제품 저장소가 필요하다. 각 upstream은 commit으로 잠그고 patches, 업데이트 시험, SBOM/고지, rollback을 관리한다. 세 저장소의 모든 훅·메모리·스킬·루프를 동시에 켜는 방식은 충돌과 반복 입력 비용을 키울 수 있다.

## 검증 계획

1. 같은 코딩 작업과 고정 완료 기준을 준비한다.
2. baseline / Ponytail lite / full을 분리된 프로필에서 비교한다.
3. 전역 플러그인이 baseline에 섞이지 않았는지 context manifest로 확인한다.
4. 코드량 외 실제 동작·테스트·브라우저·안전 요구를 검증한다.
5. 초기 입력, 전체 실행, 재시도, 검수, hook 지침 비용을 합산한다.
6. 실패·timeout을 비용과 성공률에 포함하고 제외 조건을 공개한다.
7. 품질이 유지되고 완료 업무당 사용량이 줄어드는 범위에서만 채택한다.

현재 권고: Ponytail의 원리를 코딩 경로에 제한적으로 반영하고, 독립된 자체 AI 팀 저장소를 운영 원본으로 삼는다. 실제 fork 생성·설치·모델 호출 benchmark는 이번 요청에서 수행하지 않았다.

Lupus 1.4에서는 Prime를 Alpha의 학습 기반으로 명시했다. 이 표의 통합 판단은 설계이며 upstream이 기존 CLI와 무설정으로 호환된다는 확인은 아니다. 상세는 [전체 설계](LUPUS-PLAN.md) 3·12를 따른다.
