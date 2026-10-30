"""V0.5 regression: DXF->floor on-disk bin, cache restore, upload HTTP and fallback."""
import hashlib
import http.client
import json
import sys
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import ezdxf
import web_trial.disk_cache as disk
from web_trial.server import import_dxf,CATALOG,DXF_JOBS,Handler

with tempfile.TemporaryDirectory() as tmp:
    base=Path(tmp)
    disk.CACHE_ROOT=base/'cache'
    doc=ezdxf.new('R2010');msp=doc.modelspace()
    frame='--■ DXF_공통_도면구분';doc.layers.new(frame)
    doc.layers.new('A-WALL')
    block=doc.blocks.new('SAMPLE_WINDOW');block.add_line((0,0),(1000,1000),dxfattribs={'layer':'A-WALL'})
    for name,x in [('B2F',0),('1F',20000),('2F',40000)]:
        msp.add_lwpolyline([(x,0),(x+12000,0),(x+12000,9000),(x,9000)],close=True,dxfattribs={'layer':frame})
        msp.add_text(f'{name} 평면도',dxfattribs={'insert':(x+100,8000),'layer':frame})
        msp.add_line((x+100,100),(x+500,100),dxfattribs={'layer':'A-WALL'})
        msp.add_blockref('SAMPLE_WINDOW',(x+2000,3000))
    dxf=base/'sample.dxf';doc.saveas(dxf)
    key=disk.signature_for_path(dxf)
    index,stats=import_dxf(dxf,dxf.name,key)
    assert [f['name'] for f in index['floors']]==['B2F','1F','2F'],index['floors']
    assert len(index['floors'])==3
    assert all(f['segments']>0 for f in index['floors'])
    assert all((disk.CACHE_ROOT/key/f['directory']/'lines.bin').exists() for f in index['floors'])
    assert disk.load_index(key)['title']==dxf.name
    CATALOG.set_direct_cache(disk.load_index(key))
    names=[f['name'] for f in CATALOG.floors('direct')['floors']]
    assert names==['B2F','1F','2F'],names
    for fl in names:
        s=CATALOG.prepare_scene('direct',fl)
        assert s['totalLines']>0 and len(s['bin'])==s['totalLines']*20
        assert s['totalTexts']>=1
        found=disk.pick_disk(s,101,101,set(),500)
        assert found and found['layer'] in ('A-WALL',frame),found
    assert len(list((disk.CACHE_ROOT/key).glob('floor_*/lines.bin')))==3
    # same source: cached job must return synchronously without parsing
    result=DXF_JOBS.begin(dxf,dxf.name,False,key)
    status=DXF_JOBS.status(result['id'])
    assert status['state']=='done' and status['result']['cacheHit']
    assert 'elapsedSeconds' in status and status['elapsedSeconds'] >= 0
    # Finished jobs must report a fixed wall-clock duration (not keep increasing).
    import time
    finished_elapsed=status['elapsedSeconds']
    time.sleep(.05)
    assert DXF_JOBS.status(result['id'])['elapsedSeconds']==finished_elapsed
    # Fresh startup restores existing disk index without DXF file
    dxf.unlink()
    assert disk.latest_index()['signature']==key
    # no custom frame -> spatial fallback, but retain small ordinary DXF as '전체'
    other=base/'without-frame.dxf';z=ezdxf.new('R2010');z.modelspace().add_line((0,0),(300,400));z.saveas(other)
    second,_=import_dxf(other,other.name,disk.signature_for_path(other))
    assert [v['name'] for v in second['floors']]==['전체']
    # HTTP API: scene, geometry, texts, pick via completed disk index
    CATALOG.set_direct_cache(disk.load_index(key))
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def get(url):
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
        conn.request('GET',url);res=conn.getresponse();body=res.read();code=res.status;conn.close();return code,body
    try:
        code,body=get('/api/floors?id=direct')
        assert code==200 and len(json.loads(body)['floors'])==3
        code,body=get('/api/scene?id=direct&floor=1F')
        scene=json.loads(body);assert code==200 and scene['totalLines']>0
        code,body=get('/api/geometry?id=direct&floor=1F')
        assert code==200 and len(body)==scene['totalLines']*20
        code,body=get('/api/texts?id=direct&floor=1F')
        assert code==200 and len(json.loads(body))>=1
        code,body=get('/api/pick?id=direct&floor=1F&x=101&y=100&tol=200')
        assert code==200 and json.loads(body)['selected']
    finally:server.shutdown();server.server_close()
    print('PASS V0.5: frame split, B2F order, disk persistence, cache hit, local HTTP, geometry and pick, no-frame fallback')
