"""DXF 열기 단일 버튼: PC 파일 선택 창 → 경로 직접 읽기 (/api/dxf/browse)."""
from pathlib import Path
import tempfile, sys, threading, http.client, json, time, base64, os, subprocess
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import ezdxf
import web_trial.disk_cache as disk
import web_trial.server as server_mod
from web_trial import file_dialog
from web_trial.server import CATALOG, DXF_JOBS, Handler, _read_choices, _save_choices

with tempfile.TemporaryDirectory() as d:
    root=Path(d)
    disk.CACHE_ROOT=root/'cache'
    server_mod.CACHE_ROOT=disk.CACHE_ROOT
    folder=root/'도면 폴더'
    folder.mkdir()
    dxf=folder/'평면도.dxf'
    doc=ezdxf.new('R2010')
    doc.modelspace().add_line((0,0),(2000,0))
    doc.saveas(dxf)
    text=folder/'메모.txt'
    text.write_text('not dxf',encoding='utf-8')

    calls=[]
    answer={'path':None}
    def fake_dialog(initial_dir=''):
        calls.append(initial_dir)
        return answer['path']
    real_dialog=file_dialog.ask_dxf_path
    file_dialog.ask_dxf_path=fake_dialog

    srv=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=srv.serve_forever,daemon=True).start()
    def request(path,method='GET',body=None):
        conn=http.client.HTTPConnection('127.0.0.1',srv.server_port,timeout=15)
        conn.request(method,path,body=body,headers={'Content-Type':'application/json'})
        r=conn.getresponse();b=r.read();code=r.status;conn.close()
        return code,json.loads(b)
    def wait(jobid):
        for _ in range(90):
            _,j=request('/api/dxf/job?id='+jobid)
            if j['state']!='running':return j
            time.sleep(.1)
        raise AssertionError('job timeout')
    try:
        # 1) 취소: 작업이 시작되지 않는다.
        status,body=request('/api/dxf/browse','POST',b'{}')
        assert status==200 and body=={'canceled':True},(status,body)
        assert DXF_JOBS.job is None

        # 2) DXF가 아닌 파일은 거절.
        answer['path']=text
        status,body=request('/api/dxf/browse','POST',b'{}')
        assert status==400 and 'DXF' in body['error'],(status,body)

        # 3) 고른 경로를 복사 없이 직접 읽고, 경로 기반 캐시를 만든다.
        answer['path']=dxf
        status,body=request('/api/dxf/browse','POST',b'{}')
        assert status==202 and body['title']=='평면도.dxf',(status,body)
        j=wait(body['id'])
        assert j['state']=='done' and not j['result'].get('cacheHit'),j
        key=disk.signature_for_path(dxf)
        assert CATALOG.direct_index['signature']==key
        assert Path(CATALOG.direct_index['sourcePath'])==dxf and dxf.is_file()

        # 4) 다음에 열 때는 직전 도면 폴더에서 창이 열린다. 같은 파일은 캐시 재사용.
        status,body=request('/api/dxf/browse','POST',b'{}')
        assert Path(calls[-1])==folder,calls
        assert wait(body['id'])['result'].get('cacheHit')

        # 5) 업로드 캐시(원본 위치 없음)에서 고른 도곽 선택은 carryChoices로 경로 캐시에 이어 받는다.
        upload_key='ab'*32
        _save_choices(upload_key,{'1F':[0,0,10,10]})
        CATALOG.direct_index=dict(CATALOG.direct_index,signature=upload_key,sourcePath=str(root/'gone.dxf'))
        status,body=request('/api/dxf/browse','POST',json.dumps({'force':True,'carryChoices':True}).encode())
        assert status==202,(status,body)
        j=wait(body['id'])
        assert j['state']=='done' and not j['result'].get('cacheHit'),j   # force=True → 재분석
        assert _read_choices(key)=={'1F':[0,0,10,10]},_read_choices(key)
        assert Path(calls[-1])==root, '원본 파일이 없어도 그 폴더가 남아 있으면 거기서 연다'
    finally:
        file_dialog.ask_dxf_path=real_dialog
        srv.shutdown();srv.server_close()

# 6) 창이 이미 열려 있으면 두 번째 요청은 거절.
file_dialog._LOCK.acquire()
try:
    try:file_dialog.ask_dxf_path('')
    except ValueError as e:assert '이미 열려' in str(e)
    else:raise AssertionError('second dialog allowed')
finally:file_dialog._LOCK.release()

# 7) Windows: 실제 PowerShell 스크립트가 실행되고 한글 경로가 깨지지 않는지(창 표시 부분만 제외).
if sys.platform=='win32':
    script=(file_dialog._PS_SCRIPT
            .replace('$owner.Show()','').replace('$owner.Activate()','')
            .replace('$result = $dialog.ShowDialog($owner)','$result = [System.Windows.Forms.DialogResult]::OK; $dialog.FileName = $env:DXF_DIALOG_DIR'))
    encoded=base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    probe='D:\\도면 폴더\\지하 2층 평면도.dxf'
    done=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-STA','-ExecutionPolicy','Bypass','-EncodedCommand',encoded],
                        capture_output=True,env=dict(os.environ,DXF_DIALOG_DIR=probe),timeout=60)
    assert done.returncode==0,done.stderr.decode('utf-8','replace')
    assert done.stdout.decode('utf-8-sig').strip()==probe,done.stdout

print('PASS DXF 열기: 취소, DXF 외 거절, 경로 직접 읽기, 직전 폴더, 캐시 재사용, 도곽 선택 이어받기, 중복 창 차단, 한글 경로')
