"""Streaming cache stress: 210k segments -> spatial fallback -> persisted selectable regions."""
import sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import web_trial.disk_cache as dc
with tempfile.TemporaryDirectory() as td:
    dc.CACHE_ROOT=Path(td)/'cache'
    # 240k segments with 4 spatial zones
    seg=[]
    for i in range(240000):
        x=float((i%600)*30);y=float((i//600)*30)
        seg.append((x,y,x+8,y+8,255,255,255,None,'ABCD',{'layer':'TEST'}))
    t=time.time()
    m=dc.write_cache('a'*64,'bulk.dxf',Path(td)/'bulk.dxf',[],{},seg,[],[],[],{})
    assert m['spatialFallback'] and len(m['floors'])>1,m
    assert sum(f['segments'] for f in m['floors'])>=240000
    for f in m['floors']:
        scene=dc.floor_scene(m,f['name']);assert scene['totalLines']*20==len(scene['bin'])
    print('PASS: 240000 segments, spatial fallback ',len(m['floors']),'regions,',round(time.time()-t,2),'s')
