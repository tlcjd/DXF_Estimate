"""V0.5.6: auto discovery with no explicit frames, mixed manual, and user choice API."""
import sys, tempfile, threading, time, json, http.client
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import ezdxf
import web_trial.disk_cache as dc
import web_trial.server as srvmod
from app.rooms.core import read_drawing_divisions
from web_trial.auto_frames import discover_plan_frames

with tempfile.TemporaryDirectory() as temp:
    root=Path(temp);dc.CACHE_ROOT=root/'cache';srvmod.CACHE_ROOT=dc.CACHE_ROOT
    d=ezdxf.new('R2010');m=d.modelspace()
    d.layers.new('SHEET');d.layers.new('WALL')
    for idx,title in enumerate(['1층 평면도','1층 평면도','지붕 평면도','옥탑지붕 평면도']):
        x=16000*idx
        m.add_lwpolyline([(x,0),(x+12000,0),(x+12000,9000),(x,9000)],close=True,dxfattribs={'layer':'SHEET'})
        m.add_text(title,dxfattribs={'layer':'SHEET','insert':(x+10000,100),'height':180})
        m.add_line((x+1500,2000),(x+9000,2000),dxfattribs={'layer':'WALL'})
    result,diag=discover_plan_frames(m,[])
    assert diag['autoAdded']==3 and diag['alternativeCount']>=1,diag
    floors={n for f in result for n in f['floors']}
    assert floors=={'1F','RF','PHR'},floors
    # A partial/manual layout keeps the designated 1F while adding roof floors.
    manual=[dict(result[0],auto_detected=False)]
    merged,diag2=discover_plan_frames(m,manual)
    assert diag2['autoAdded']==2,diag2
    path=root/'sample.dxf';d.saveas(path)
    key=dc.signature_for_path(path)
    index,stats=srvmod.import_dxf(path,path.name,key)
    assert set(x['name'] for x in index['floors'])=={'1F','RF','PHR'}
    srvmod.CATALOG.set_direct_cache(index)
    server=srvmod.ThreadingHTTPServer(('127.0.0.1',0),srvmod.Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    def request(uri,method='GET',data=None):
        con=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=20)
        body=json.dumps(data).encode('utf-8') if data is not None else None
        con.request(method,uri,body,{'Content-Type':'application/json'} if body else {})
        res=con.getresponse();raw=res.read();status=res.status;con.close();return status,json.loads(raw)
    try:
        candidate=stats['frameDetection']['alternatives'][0]
        status,job=request('/api/dxf/frame/choose','POST',{'floor':candidate['floors'][0],'rect':candidate['rect']})
        assert status==202,(status,job)
        for _ in range(100):
            _,latest=request('/api/dxf/job?id='+job['id'])
            if latest['state']!='running':break
            time.sleep(.1)
        assert latest['state']=='done',latest
        final=dc.load_index(key)
        assert final['stats']['frameDetection']['manualSelections'],final['stats']['frameDetection']
        _,frames=request('/api/dxf/frames')
        assert frames['manualSelections']
        status,reset=request('/api/dxf/frame/reset','POST',{'floor':'1F'})
        assert status==202,(status,reset)
        for _ in range(100):
            _,latest=request('/api/dxf/job?id='+reset['id'])
            if latest['state']!='running':break
            time.sleep(.1)
        assert latest['state']=='done',latest
        assert not dc.load_index(key)['stats']['frameDetection']['manualSelections']
        print('PASS V0.5.6 zero/manual mixed frames; RF/PHR; alternatives; choose/reset API; persisted cache')
    finally:
        server.shutdown();server.server_close()

# No drawn rectangle at all: a regular plan-title grid yields marked,
# review-required virtual frames instead of an unsplit multi-million-line scene.
d=ezdxf.new('R2010');m=d.modelspace()
for idx,title in enumerate(['B2F 평면도','1F 평면도','2F 평면도']):
    m.add_text(title,dxfattribs={'insert':(20000*idx+15000,0),'height':180,'layer':'TITLE'})
    m.add_line((idx*20000+1000,3000),(idx*20000+13000,3000))
frames,report=discover_plan_frames(m,[])
assert report['autoAdded']==3 and all(a['confidence']=='추정·확인필요' for a in report['added']),report
print('PASS unframed three-plan title grid -> review-required virtual boundaries')
