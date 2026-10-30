# -*- coding: utf-8 -*-
"""Regression: automatic rectangles/title pairing, non-invasive fallback."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import ezdxf
from web_trial.auto_frames import discover_plan_frames
from web_trial.server import import_dxf
from web_trial import disk_cache as dc


def draw_box(msp,x,y,w,h,layer,poly=True):
    corners=[(x,y),(x+w,y),(x+w,y+h),(x,y+h)]
    if poly:msp.add_lwpolyline(corners,close=True,dxfattribs={'layer':layer})
    else:
        for a,b in zip(corners,corners[1:]+corners[:1]):
            msp.add_line(a,b,dxfattribs={'layer':layer})


with tempfile.TemporaryDirectory() as d:
    d=Path(d);dc.CACHE_ROOT=d/'cache'
    doc=ezdxf.new('R2010')
    doc.layers.new('FRAME');doc.layers.new('A-WALL')
    msp=doc.modelspace()
    draw_box(msp,0,0,12000,9000,'FRAME',poly=True)
    msp.add_text('지하3층 평면도',dxfattribs={'layer':'FRAME','insert':(200,8400),'height':160})
    draw_box(msp,18000,0,12000,9000,'FRAME',poly=False)
    msp.add_text('PLAN',dxfattribs={'layer':'FRAME','insert':(18100,8400),'height':160})
    msp.add_text('1F',dxfattribs={'layer':'FRAME','insert':(18800,8400),'height':160})
    draw_box(msp,36000,0,12000,9000,'FRAME',poly=True)
    msp.add_text('2F 평면도',dxfattribs={'layer':'FRAME','insert':(36300,8400),'height':160})
    draw_box(msp,54000,0,12000,9000,'FRAME',poly=True)
    msp.add_text('SHOP DRAWING',dxfattribs={'layer':'FRAME','insert':(54300,8400),'height':160})
    for i,x in enumerate((0,18000,36000,54000)):
        msp.add_line((x+1000,2000),(x+7000,2000),dxfattribs={'layer':'A-WALL'})
    result,diagnostics=discover_plan_frames(msp,[])
    names={n for r in result for n in r.get('floors',[])}
    assert names=={'B3F','1F','2F'},(names,diagnostics)
    assert diagnostics['autoAdded']==3,diagnostics
    assert any(x['source']=='LINE 4개' for x in diagnostics['added']),diagnostics
    assert len(result)==3,diagnostics
    dxf=d/'sample.dxf';doc.saveas(dxf)
    index,stats=import_dxf(dxf,'sample.dxf',dc.signature_for_path(dxf))
    assert [x['name'] for x in index['floors']]==['B3F','1F','2F'],index['floors']
    assert all(x['segments']>0 for x in index['floors'])
    assert stats['frameDetection']['autoAdded']==3
    # Legacy author-annotated frames remain authoritative, no duplicate.
    mark='--■ DXF_공통_도면구분';doc.layers.new(mark)
    draw_box(msp,72000,0,12000,9000,mark)
    msp.add_text('3F 평면도',dxfattribs={'layer':mark,'insert':(72300,8400),'height':160})
    from app.rooms.core import read_drawing_divisions
    old=read_drawing_divisions(msp)
    merged,diag=discover_plan_frames(msp,old)
    assert '3F' in {s for r in merged for s in r.get('floors',[])}
    assert diag['autoAdded']==3,diag
    print('PASS V0.5.3: mixed polyline/4 LINE frames, split title, no false floor, legacy preference, persisted cache')
