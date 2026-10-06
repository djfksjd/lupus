我的 판단은 **P6를 먼저 올리고, P1의 완료 판정 설계를 바꿔야 한다**는 것이다. 지금 Lupus의 가장 큰 위험은 비용이 조금 더 드는 것이 아니라, 약한 검증기를 자동으로 붙여 **하지 않은 일을 완료했다고 확정하는 것**이다.

파일은 읽기만 했다. 문서·실측 JSON·관련 코드를 확인했으며, 시험이나 CLI worker는 실행하지 않았다.

**1. 현재 구현과 오픈소스 비교**

먼저 현재 상태에서 두 가지를 바로잡아야 한다.

- **P2의 핵심은 이미 코드에 있다.** `_last_failures()`가 실패 증거를 가져오고, 재시도 prompt에서 자체 확인 금지를 푼다. 대응 시험도 있다. 다만 Claude의 기본 도구는 여전히 `Read/Write/Edit`라서 **테스트 실행까지 허용된 것은 아니다.** [supervisor.py](<lupus/src/lupus/supervisor.py:145>), [adapters.py](<lupus/src/lupus/adapters.py:144>)
- **“사람의 수고 0”은 측정되지 않았다.** 비교 스크립트는 Lupus 쪽 `human_prompt_chars=0`을 직접 기록한다. 목표·task·검증기를 작성하는 수고는 포함하지 않는다. 인계 설명을 자동화했다는 근거는 있지만, 전체 준비 비용을 없앴다는 근거는 아니다. [compare.py](<lupus/evaluations/compare.py:248>)

웹 비교는 공식 저장소·문서 기준이다. 기능 제공과 유지 상태를 확인했으며, 모든 프로젝트의 실제 활성 사용자 수나 성능을 독립 검증한 것은 아니다. 아래 **“드문 점”은 공개 문서에서 확인한 차이에 대한 추정**이지, 상대 코드에 해당 기능이 절대 없다는 뜻이 아니다.

| 부류·프로젝트 | Lupus보다 확실히 앞선 점 | Lupus가 가진 상대적으로 드문 점 |
|---|---|---|
| 대화형 코딩: [aider](https://github.com/Aider-AI/aider) | 대화형 수정, 저장소 지도, Git 연동. lint/test 실패를 편집 루프에 직접 되먹임한다. [검증 문서](https://aider.chat/docs/usage/lint-test.html) | 설치된 Claude/Codex를 바꿔도 목표·예산·시도·증거 revision을 하나의 로컬 원장으로 유지하는 조합은 드물다. **자동 테스트 자체는 차별점이 아니다.** |
| 실행 플랫폼: [OpenHands](https://github.com/OpenHands/OpenHands), [SDK](https://github.com/OpenHands/software-agent-sdk) | 현재 Agent Canvas와 SDK로 분리돼 있다. 대화 UI, 여러 agent/backend, Docker·VM 실행, MCP·스킬·자동화 연결을 제공한다. | 표준 라이브러리 중심의 작은 supervisor와 구체적인 예산 예약·불명 사용량 처리 계약. 단, 작다는 것이 성능 우위를 뜻하지 않는다. |
| 문제 해결·평가: [SWE-agent](https://github.com/SWE-agent/SWE-agent), [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | 실제 저장소 문제를 대상으로 한 평가 기반, trajectory 분석, 여러 실행 환경. SWE-agent는 현재 mini를 후속 기본 선택으로 권장한다. | native CLI 간 인계에서도 시도·예산을 초기화하지 않고 완료 증거를 이어가는 운영 계약. |
| 요구·계획: [Task Master](https://github.com/eyaltoledano/claude-task-master), [spec-kit](https://github.com/github/spec-kit), [BMAD](https://github.com/bmad-code-org/BMAD-METHOD) | 요구를 계획·task로 바꾸는 사용자 흐름과 기존 도구 연결. Task Master도 Claude/Codex CLI 인증 경로를 제공한다. | 모델이 task 상태를 선언하는 것과 별개로 supervisor가 acceptance revision·artifact hash에 묶인 증거로 DONE을 결정한다. |
| 코딩 절차: [superpowers](https://github.com/obra/superpowers) | 평소 agent 안에서 요구 명료화, 계획, TDD, 검토 절차를 적용한다. Lupus의 W1·W2에 해당하는 진입 경험이 앞선다. | prompt 준수에만 의존하지 않는 예산·시도·프로세스 종료·완료 판정의 프로그램 계약. |
| 오케스트레이션: [Ruflo](https://github.com/ruvnet/ruflo), [oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode) | CLI·훅·MCP 통합, 역할별 agent, 팀 실행, 기억·검토 흐름. OMC는 실제 명령 출력과 신선한 증거를 요구하며 crash recovery도 제공한다. [구조](https://github.com/Yeachan-Heo/oh-my-claudecode/blob/main/docs/ARCHITECTURE.md), [릴리스](https://github.com/Yeachan-Heo/oh-my-claudecode/releases) | 작은 비모델 supervisor에 완료·인계·예산 불변식을 집중한 구성. **“저들은 증거·복구가 없다”는 주장은 틀리다.** 같은 강도의 계약인지 여부는 추가 코드 비교가 필요하다. |
| 세션·작업공간: [claude-squad](https://github.com/smtg-ai/claude-squad), [Vibe Kanban](https://github.com/BloopAI/vibe-kanban), [Crystal](https://github.com/stravu/crystal) | 대화형 세션 관리, worktree 분리, diff 검토·피드백. Vibe Kanban은 커뮤니티 유지로 전환됐고, Crystal은 Nimbalyst로 대체됐다. [종료 공지](https://www.vibekanban.com/blog/shutdown) | 세션 관리보다 강한 목표 단위의 증거·예산·재시도 연속성. 반대로 Lupus는 변경 검토와 작업공간 분리가 부족하다. |
| 사용량: [ccusage](https://github.com/ccusage/ccusage) | 평소 CLI의 로컬 기록을 읽어 여러 도구의 일별·세션별 사용량을 보여준다. 실행 습관을 바꾸지 않아도 된다. | 호출 **전** 예산 예약과 이후 실행 차단, 사용량 불명 시 보수적 정산. 관측 도구와 집행 도구의 차이다. |
| 기억: [AgentMemory](https://github.com/AndrewMoryakov/AgentMemory), [mem0](https://github.com/mem0ai/mem0), [Letta](https://github.com/letta-ai/letta), [Graphiti](https://github.com/getzep/graphiti) | 공유 CLI/HTTP/MCP, 의미 검색, 지속 대화, 시간에 따른 사실·출처 관리 등 각자의 전문 기능이 앞선다. AgentMemory는 public alpha이며, mem0의 관리형 성능 수치는 OSS SDK와 구분해야 한다. | 모델 추가 호출 없이 실행 증거와 기억의 사용 이력을 연결한다. 다만 “통과한 시도에 포함됐다”는 상관만으로 기억의 유용성을 입증하지는 못한다. |
| 지속 실행 기반: [LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence) | checkpoint·재개·사람 개입·지속 기억을 위한 범용 기반이 이미 있다. | native CLI를 대상으로 예산·writer·완료 증거를 바로 적용하는 완성된 좁은 계약. **checkpoint 자체는 독창적이지 않다.** |

[Conductor](https://www.conductor.build/)도 native agent 연결, 협업, cloud microVM 실행에서 비교 가치가 있다. 다만 확인한 공식 페이지에서는 제품 전체의 공개 소스·라이선스를 확인하지 못했으므로 OSS 표에는 합치지 않았다.

**“모든 오픈소스보다 낫다”는 목표는 현실적이지 않다.** 서로 다른 문제를 푸는 제품을 하나의 순위로 묶을 수 없다. Lupus가 겨룰 만한 목표는 다음처럼 좁아야 한다.

> 검증 가능한 작업을 Claude↔Codex로 중단·재개할 때, 완료 신뢰도를 유지하면서 사람의 재설명 시간과 총 실행 비용을 줄인다.

**2. P1~P6에 대한 반론**

| 후보 | 반대 의견·위험 | 판단 |
|---|---|---|
| **P1 한 줄 요청 + 테스트 탐지** | 테스트 명령 발견과 요청의 완료 조건 발견은 다르다. “CSV 내보내기 추가” 전에도 기존 테스트는 통과할 수 있다. monorepo·환경 설정·빈 테스트·무의미한 `npm test`도 문제다. 한 번 확인받은 기본 명령은 프로젝트 회귀 검사이지 모든 미래 요청의 검증기가 아니다. | **현재 제안 그대로는 함정.** 먼저 기존 실패 테스트 복구로 범위를 제한하라. |
| **P2 실패 출력 + 충실한 재시도** | 핵심은 이미 구현됐다. 그러나 prompt 변경만으로 Bash·MCP 능력이 생기지 않는다. 출력 끝부분에 원인이 없을 수 있고, 출력은 비밀이나 조작된 지시를 포함할 수 있다. 환경 장애를 코드 실패로 분류하면 재시도도 낭비다. | **작은 보완 후 실측.** retry의 실제 도구·권한·비용까지 확인해야 한다. |
| **P3 사전 통과 건너뛰기 + 묶음** | 둘은 별도 기능이다. P1의 기존 테스트를 사용하면 요청 미수행 상태에서 DONE이 된다. 또 검증 명령도 파일을 쓰므로 **claim 전 무잠금 실행은 현재 writer 계약과 충돌**한다. 묶음은 뒤 작업을 미리 수행해 의존·승인·checkpoint 경계를 흐릴 수 있다. | 건너뛰기는 요청별 조건이 있을 때만. 묶음은 단순 조건부 skip 대신 명시적 실행 단위로 설계하라. |
| **P4 다른 모델의 판정** | 다른 모델이라고 오류가 독립적이지 않다. 같은 자료·잘못된 rubric을 공유하면 함께 틀린다. 근거 인용의 존재와 근거의 정확성도 다르다. worker가 바꾼 문서가 reviewer를 유도할 수 있다. | **DONE 검증기로는 보류.** 우선 비차단 검토 의견으로 평가하라. 확률적 판단을 결정적 증거처럼 표시하면 안 된다. |
| **P5 전역 연결 설계·설치기만** | 설치기를 설계하는 것부터 비용이다. 현재 headless·고정 도구 경로에 전역 훅을 붙여도 대화형 작업은 해결되지 않는다. 사용자 설정과 Lupus 권한 계약의 소유권도 충돌한다. | 보류는 맞다. **설치기 작업도 지금은 빼라.** 명시적 단일 진입점부터 입증하라. |
| **P6 어려운 작업 benchmark** | 어려운 작업만 추가하면 부족하다. 현재 조건 순서는 고정이며 캐시·준비 시간·검증기 작성 비용·잘못된 PASS를 충분히 분리하지 않는다. | **최우선.** 기능 추가의 마지막 검사가 아니라 우선순위를 정하는 실험이어야 한다. |

특히 **P1→P3→P4는 위험한 순서**다. 약한 조건을 자동 생성하고, 그 조건이 통과하면 생략하고, 남은 의미 판단을 모델에게 맡기는 흐름은 Lupus의 장점을 스스로 없앨 수 있다.

**3. 내가 우선할 고도화 5개**

아래 수치는 **제안하는 채택 기준**이며 달성한 결과가 아니다. 기준을 못 넘으면 기본 기능으로 넣지 않는다.

| 순서·항목 | 최소 구현 범위 | 완료·이득 판정 |
|---|---|---|
| **① 절감 원인과 품질을 분리하는 평가** | 같은 파일 동봉·prompt·도구 설정을 쓰는 직접 CLI 기준선을 추가. 작업당 반복, 조건 순서 무작위화, CLI/model 고정. 준비·검토·재시도 시간을 포함하고 별도 holdout 검사 사용. | 난이도별 성공률·잘못된 PASS·성공당 비용·사람 시간을 보고할 수 있어야 한다. supervisor 없이도 같은 이득이면 절감 공로를 그쪽으로 돌린다. |
| **② 검증 대상과 검증 수단의 분리** | 실행 전 테스트·runner·설정 snapshot. 별도 검증 checkout에 허용된 결과 변경만 적용하고 원래 검증 수단으로 검사. 첫 범위는 Git 저장소·로컬 테스트로 제한. | 테스트 삭제·skip·runner 약화·설정 변경·미선언 의존 변경 사례를 모두 잡는다. **잘못된 PASS 감소**와 검증 시간 증가를 함께 측정한다. |
| **③ 목표 파일 없는 ‘실패 테스트 복구’** | 프로젝트 탐지, 기존 테스트 명령 선택, baseline 실패 수집, 단일 task·예산·검증 조건 자동 생성. 일반 기능 추가는 아직 지원하지 않는다. | 지원 프로젝트에서 사람의 JSON·검증기 작성 **0회**, 이후 실행 준비 시간 중앙값 30초 이하. 환경 장애·테스트 0개를 완료로 처리하지 않는다. |
| **④ 실제 능력이 바뀌는 재시도** | 기존 실패 feedback 유지. 제한된 읽기·확인 또는 승인된 테스트 실행 수단 제공. 환경 실패는 별도 분류. 출력 정제·길이 제한·실패 증거 ID 기록. | 첫 시도 실패 작업에서 추가 성공률이 개선되고, 같은 성공률을 얻는 직접 CLI 대비 총 재시도 비용이 감소해야 한다. |
| **⑤ 검증 경계를 보존하는 호출 묶음** | 같은 프로젝트·driver·권한·예산의 작은 연속 작업만 한 실행 단위로 묶는다. worker 종료 후 조건별 검사, 일부 실패면 실패 범위만 재개. 중간 승인·외부 효과는 제외. | 분리 실행 대비 성공당 토큰 20% 이상 감소, 잘못된 PASS 증가 없음, 중단 후 재작업 시간 악화 없음. 고정 입력만 보고 기본값으로 켜지 않는다. |

②의 checkout 분리는 **변경 충돌과 검증 오염을 줄이는 장치**다. 같은 UID의 악성 worker를 막는 보안 격리라고 부르면 안 된다.

현재 `artifact_hash()`는 선언된 파일만 보고, 디렉터리는 재귀 내용 대신 `missing`으로 취급한다. 자동 검증에서 `paths=["src", "tests"]`를 넣는 식으로 해결하면 증거가 실제 내용을 대표하지 못한다. [verify.py](<lupus/src/lupus/verify.py:27>)

**4. W1을 없애는 가장 작은 설계**

**“테스트를 자동 발견하면 임의의 한 줄 요청을 검증할 수 있다”는 전제부터 버려야 한다.** 요청의 의미를 판단할 기준은 어디선가 와야 한다.

가장 작은 정직한 제품은 다음이다.

```text
lupus do "현재 실패하는 테스트를 고쳐줘"
```

실행 흐름은 여섯 단계면 된다.

1. 현재 폴더에서 프로젝트와 테스트 명령 후보를 탐지한다. 첫 사용에서 실행 명령과 범위를 선택받아 저장한다.
2. worker 전에 supervisor가 실행해 **실제 테스트가 발견됐고 어떤 assertion이 실패했는지** 기록한다.
3. 그 실패를 목표로 삼아 단일 task·예산·검증 조건을 자동 생성한다.
4. 테스트·runner·검증 설정을 기준본으로 고정하고 worker에는 실패 내용과 관련 코드만 준다.
5. 별도 검증 작업공간에 결과를 적용하고, **고정한 검사로 원래 실패와 회귀를 다시 확인**한다.
6. 기준본 변경·검사 누락·환경 실패가 없고 조건을 통과할 때만 DONE으로 만든다.

worker의 “고쳤다”와 worker가 제출한 테스트 출력은 증거로 쓰지 않는다. 증거는 supervisor가 관측한 명령·테스트 목록·종료 결과·검증 대상 hash다.

baseline이 이미 통과한다면 **“실패 테스트 없음”**이라고 보고해야 한다. 그것이 “한 줄 요청 완료”를 뜻하지는 않는다.

일반 기능 추가까지 넓히려면 최소한 다음 중 하나가 필요하다.

- 이미 존재하는 요청별 acceptance test
- 사용자가 제시한 입력·예상 출력으로 만드는 검사
- 실행 전에 생성·검토·고정한 요청별 검사

별도 AI가 테스트를 만들어도 사람의 **작성 노동**은 줄일 수 있지만, 요청을 제대로 이해했다는 보장은 생기지 않는다. 따라서 **검증기 코드 작성 0회는 가능하지만, 모든 임의 요청에서 의미 확인까지 0회이면서 신뢰도도 유지한다는 약속은 불가능하다.**

**5. 하나를 고른다면, 그리고 버릴 것**

하나를 고른다면 **“검증된 진행 상태를 보존하는 Claude↔Codex 인계”**다.

차별화할 것은 CLI 교체 버튼이 아니다. **무엇을 검사했는지, 무엇이 아직 미검증인지, 얼마나 썼는지, 어떤 실패를 반복했는지를 교체 후에도 잃지 않는 것**이다. 기존 구현의 예산·attempt·checkpoint·acceptance revision이 이 방향에 가장 잘 맞는다. [CONTRACT](<lupus/docs/CONTRACT.md>)

이를 입증하려면 단순 토큰 절감보다 **중단 후 재설명 시간, 완료 작업 재실행 수, 인계 후 잘못된 완료 수**를 먼저 재야 한다.

버릴 것은 다음이다.

- 모든 작업·모든 오픈소스를 이기겠다는 목표
- 기존 테스트 통과를 임의 요청 완료로 바꾸는 P1
- 다른 모델의 동의를 결정적 PASS로 바꾸는 P4
- 근거 없는 기본 task 세분화와 기본 “한 번에 쓰기”
- 당장의 전역 훅·swarm·기억 그래프 확장

Claude에게 가장 강하게 반대할 지점은 이것이다. **Lupus를 쉽게 쓰게 만드는 작업보다, 쉽게 붙인 검증기가 거짓 완료를 만들지 않게 하는 작업이 먼저다.**