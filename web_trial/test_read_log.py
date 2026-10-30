"""V0.5.4 read-log smoke test, standalone and independent of real CAD files."""
import http.client
import json
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from web_trial import read_log
from web_trial.server import DXF_JOBS,Handler


def get_log(port):
    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=10)
    conn.request('GET','/api/dxf/log')
    response=conn.getresponse();data=response.read();status=response.status
    mime=response.getheader('Content-Type');conn.close()
    assert status==200 and 'text/plain' in mime,(status,mime)
    return data.decode('utf-8-sig')

with tempfile.TemporaryDirectory() as tmp:
    read_log.LOG_PATH=Path(tmp)/'last_dxf_read.txt'
    job={'title':'한글_평면도.dxf','started':time.time(),'_start_perf':time.perf_counter(),
         '_end_perf':None,'state':'running','stage':0,'message':'DXF 분석 중','error':'','result':None}
    read_log.start_log(job,Path(tmp)/'한글_평면도.dxf',cache_hit=False)
    read_log.record(job,'기존 DXF 엔진으로 블록·XCLIP·선분 해석 중',stage=35)
    job.update(state='done',result={'segments':2870059,'floors':[{'name':'B3F','lines':11020,'texts':33,'fills':10}],
                                    'frameDetection':{'existing':1,'autoAdded':1,'unresolvedCount':2,
                                                      'unresolved':[{'title':'2F 평면도','reason':'층 중복'}]}},
               _end_perf=time.perf_counter())
    read_log.record(job,'층별 디스크 캐시 저장 완료',stage=100)
    read_log.persist(job)
    text=read_log.latest_text()
    assert all(s in text for s in ['한글_평면도.dxf','2,870,059','B3F','11,020','층 중복',
                                  '단계별 진행 기록','도곽 자동 판정'])
    prior=DXF_JOBS.job
    DXF_JOBS.job=job
    srv=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
    try:
        assert get_log(srv.server_port)==read_log.render(job)
        job['state']='error';job['error']='ValueError: 부적절한 DXF'
        job['_traceback']='Traceback (most recent call last):\nValueError: 부적절한 DXF'
        err=get_log(srv.server_port)
        assert '예외 추적' in err and 'Traceback' in err and '부적절한 DXF' in err
    finally:
        srv.shutdown();srv.server_close();DXF_JOBS.job=prior
    assert text==read_log.latest_text()
print('PASS V0.5.4: Korean UTF-8 log, success, floor diagnostics, HTTP export, error traceback, disk persistence')
