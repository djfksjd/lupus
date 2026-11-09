"""Human terminal rendering for answer-only and explicitly unverified operations."""
from . import adapters
from .util import diagnostic_tail, language


def answer(report: dict) -> str | None:
    if report.get('route') != 'answer' and report.get('status') != 'UNVERIFIED':
        return None
    ko = language() == 'ko'
    def safe(value):
        text=str(value)
        return diagnostic_tail(text,limit=max(1,8*len(text)),multiline=True)
    lines=[]
    if report.get('route') == 'answer' and not report.get('computed_locally'):
        lines.append('읽기 전용 답변 · 미검증' if ko else 'Read-only answer · unverified')
    if report.get('status') == 'UNVERIFIED':
        lines.append('미검증 · 변경 사항을 별도로 확인하세요' if ko else 'Unverified · review the changes before relying on them')
    if report.get('result'):
        lines.append(safe(report['result']))
    if report.get('error'):
        lines.append(('오류: ' if ko else 'Error: ')+safe(report['error']))
        if report.get('detail'):lines.append(safe(report['detail']))
        if report.get('next'):lines.append(('다음: ' if ko else 'Next: ')+safe(report['next']))
    if report.get('requested_native_config') == 'user':
        confinement = 'on' if report.get('outer_os_sandbox') is True else 'unconfirmed'
        lines.append(('agent 프로필: user · 외부 OS 샌드박스: ' if ko else 'Agent profile: user · outer OS sandbox: ')+confinement)
        lines.append('내부 도구 권한: 미확인 · 사용자 설정이 요청 프로필보다 우선할 수 있음' if ko else 'Inner tool permissions: unconfirmed · user configuration may override the requested profile')
        if report.get('error') and report.get('native_config_limits'):lines.append(safe(report['native_config_limits']))
    observed=report.get('tool_diagnostics',{})
    if isinstance(observed,dict) and adapters.valid_counter(observed.get('failed')) and observed['failed']:
        count=('≥' if observed.get('counts_capped') else '')+str(observed['failed'])
        lines.append((f'작업 중 도구 오류 {count}건 관측 · 이후 복구되었을 수 있습니다' if ko else f'During work: {count} observed tool errors · may have been recovered'))
        for entry in observed.get('errors',[])[:3]:
            if isinstance(entry,dict):lines.append(safe(str(entry.get('name','tool'))+' / '+str(entry.get('error_code','TOOL_ERROR'))))
    reads=report.get('skill_reads',[])
    if isinstance(reads,list) and reads:
        ids=list(dict.fromkeys(str(row.get('id','')) for row in reads if isinstance(row,dict)))[:8]
        lines.append(('읽은 스킬 자료: ' if ko else 'Skill resources read: ')+safe(', '.join(ids))+(' · 적용 결과는 미확인' if ko else ' · application not verified'))
    identity=[report.get('requested_provider'),report.get('requested_model')]
    if any(identity):
        lines.append(('요청 설정: ' if ko else 'Requested: ')+safe(' / '.join(str(item) for item in identity if item)))
    # Local fixed answers keep their concise existing shape. Never call a lower bound a total.
    if not report.get('computed_locally'):
        raw={'usage_coverage':report['usage_coverage']} if 'usage_coverage' in report else None
        info=adapters.usage_info(report.get('usage'),raw)
        known=max(info['known_token_lower_bound'],report.get('known_token_lower_bound',0) if adapters.valid_counter(report.get('known_token_lower_bound')) else 0)
        if info['complete']:
            lines.append(('토큰: ' if ko else 'Tokens: ')+str(info['tokens']))
        else:
            lines.append((f'토큰: ≥{known} · 총량 미관측' if ko else f'Tokens: ≥{known} · total unobserved'))
    return '\n'.join(lines)
