# -*- coding: utf-8 -*-
"""Human-readable, UTF-8 DXF read diagnostics for the local web trial."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / 'data' / 'last_dxf_read.txt'


def start_log(job, source: Path, *, cache_hit: bool):
    job['_source_display'] = str(source)
    job['_events'] = []
    job['_cache_hit'] = cache_hit
    job['_traceback'] = ''
    record(job, '저장된 디스크 캐시 확인' if cache_hit else 'DXF 읽기 시작', stage=0)


def record(job, message: str, *, stage=None):
    from datetime import datetime
    import time
    stamp = datetime.now().strftime('%H:%M:%S')
    elapsed = max(0, time.perf_counter()-job['_start_perf'])
    percent = f' {int(stage):3d}%' if stage is not None else ' ---'
    line = f'[{stamp}] [+{elapsed:,.1f}s] [{percent}] {message}'
    events = job.setdefault('_events', [])
    if not events or events[-1]['message'] != message or events[-1]['stage'] != stage:
        events.append({'text':line,'message':message,'stage':stage})


def _list_lines(rows, fields):
    out=[]
    def val(value):return f'{value:,}' if isinstance(value,int) and not isinstance(value,bool) else str(value)
    for item in rows or []:
        if not isinstance(item,dict):continue
        out.append('  - '+ ' · '.join(f'{label}: {val(item.get(k, ""))}' for k,label in fields))
    return out


def render(job):
    import time
    started=job.get('started',time.time())
    done=job.get('_end_perf')
    elapsed=max(0,(done if done is not None else time.perf_counter())-job['_start_perf'])
    result=job.get('result') or {}
    output=[
        'DXF 견적프로그램 - 로컬 웹 DXF 읽기 진단 로그',
        '='*63,
        '버전: V0.5.6',
        f'도면: {job.get("title", "")}',
        f'원본: {job.get("_source_display", "")}',
        f'시작: {datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S")}',
        f'현재 상태: {job.get("state", "")} · 소요시간: {elapsed:.1f}초',
        f'결과: {"캐시 재사용" if job.get("_cache_hit") else "DXF 분석"}',
        '', '[단계별 진행 기록]',
    ]
    output += [e['text'] for e in job.get('_events',[])]
    if result:
        output += ['', '[결과 요약]']
        for k,label in [
            ('cacheHit','캐시 재사용'),('segments','분석된 원본 선분 수'),
            ('texts','문자 수'),('frames','도곽 수'),('xclips','XCLIP 감지 수'),
            ('invisibleSkipped','숨김 객체 제외 수'),('spatialFallback','공간 분할 대체 적용'),
        ]:
            if k in result:
                value=result[k]
                output.append(f'{label}: {value:,}' if isinstance(value,int) and not isinstance(value,bool) else f'{label}: {value}')
        output.append(f'층/영역 수: {len(result.get("floors") or [])}')
        output.extend(_list_lines(result.get('floors'),[('name','층'),('lines','선'),('texts','문자'),('fills','채움')]))
        if result.get('notice'):output.append('안내: '+str(result['notice']))
        if result.get('unresolvedXrefs'):output.append('미해결 XREF: '+', '.join(map(str,result['unresolvedXrefs'])))
        if result.get('emptyBlockRefs'):output.append('빈 블록: '+', '.join(map(str,result['emptyBlockRefs'])))
        frames=result.get('frameDetection') or {}
        if frames:
            output += ['', '[도곽 자동 판정]']
            for key,label in [('existing','기존 층 수'),('autoAdded','자동 추가'),('rectCandidates','사각 후보'),
                              ('planTitles','평면도 제목 후보'),('alternativeCount','동일 층 다른 도면 후보'),('unresolvedCount','판정 보류')]:
                if key in frames:output.append(f'{label}: {frames[key]}')
            for label,key in [('기존 지정','existingFrames'),('자동 판정','added'),('다른 도면 후보','alternatives'),('사용자 확정','manualSelections'),('판정 보류','unresolved')]:
                for item in frames.get(key) or []:
                    floors=', '.join(map(str,item.get('floors') or []))
                    output.append(f'  - {label}: {floors or "(층 미확정)"} · {item.get("title", "")}'
                                  f' · {item.get("source", item.get("reason", ""))}'
                                  f' · 도곽={item.get("rect", "")}')
    if job.get('error'):
        output += ['', '[오류]',str(job['error'])]
    if job.get('_traceback'):
        output += ['', '[예외 추적]',str(job['_traceback']).rstrip()]
    output.append('')
    return '\n'.join(output)


def persist(job):
    try:
        LOG_PATH.parent.mkdir(parents=True,exist_ok=True)
        LOG_PATH.write_text(render(job),encoding='utf-8-sig')
    except OSError as exc:
        print('[DXF 로그 파일 저장 실패]',exc,flush=True)


def latest_text():
    if LOG_PATH.is_file():
        return LOG_PATH.read_text(encoding='utf-8-sig')
    return 'DXF 읽기 로그가 아직 없습니다. DXF를 한 번 읽어주세요.\n'
