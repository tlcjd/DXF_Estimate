"""V0.5.5 regression: force rebuild and delete only current generated disk cache."""
from pathlib import Path
import tempfile, sys, threading, http.client, json, time
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import ezdxf
import web_trial.disk_cache as disk
import web_trial.server as server_mod
from web_trial.server import CATALOG, DXF_JOBS, Handler, import_dxf

with tempfile.TemporaryDirectory() as d:
    root=Path(d)
    disk.CACHE_ROOT=root/'cache'
    server_mod.CACHE_ROOT=disk.CACHE_ROOT
    dxf=root/'mixed.dxf'
    doc=ezdxf.new('R2010')
    doc.modelspace().add_line((0,0),(2000,0))
    doc.saveas(dxf)
    key=disk.signature_for_path(dxf)
    first,firststats=import_dxf(dxf,dxf.name,key)
    assert first['stats']['segments']==1
    cache_folder=disk.CACHE_ROOT/key
    old_created=first['created']
    second,secondstats=import_dxf(dxf,dxf.name,key)
    assert second['created']==old_created
    time.sleep(.03)
    third,thirdstats=import_dxf(dxf,dxf.name,key,force=True)
    assert third['created']>old_created
    assert third['stats']['segments']==1
    assert len(list(disk.CACHE_ROOT.glob(key+'.old.*')))==0
    assert disk.load_index(key)
    # A bad rebuild should preserve the prior valid cache.
    broken=root/'broken.dxf'
    broken.write_text('not DXF',encoding='utf-8')
    try:import_dxf(broken,'broken',key,force=True)
    except Exception:pass
    else:raise AssertionError('invalid source accepted')
    assert disk.load_index(key)['created']==third['created']
    CATALOG.set_direct_cache(disk.load_index(key))
    srv=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=srv.serve_forever,daemon=True).start()
    def request(path,method='GET',body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',srv.server_port,timeout=15)
        conn.request(method,path,body=body,headers=headers or {})
        r=conn.getresponse();b=r.read();code=r.status;conn.close()
        return code,json.loads(b)
    try:
        # Force via local-path API must produce a running job, not an immediate cache hit.
        status,body=request('/api/dxf/open-path','POST',str(dxf).encode('utf8'),{'Content-Type':'application/octet-stream','X-Force-Rebuild':'1'})
        assert status==202, (status,body)
        jobid=body['id']
        for _ in range(90):
            _,j=request('/api/dxf/job?id='+jobid)
            if j['state']!='running':break
            time.sleep(.1)
        assert j['state']=='done' and not j['result'].get('cacheHit'),j
        assert disk.load_index(key)
        status,body=request('/api/dxf/cache/delete','POST',b'1',{'Content-Type':'application/octet-stream'})
        assert status==200 and body['deleted'] and body['signature']==key,(status,body)
        assert not cache_folder.exists() and dxf.is_file()
        assert CATALOG.direct_index is None
        status,body=request('/api/dxf/cache/delete','POST',b'1',{'Content-Type':'application/octet-stream'})
        assert status==400
    finally:
        srv.shutdown();srv.server_close()
print('PASS V0.5.5: force refresh, safe overwrite, failed rebuild retention, HTTP delete, source retained')
