"""PoC V0.4: DXF -> existing CAD vector reader -> floor data -> viewer payload."""
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import ezdxf
from web_trial.server import import_dxf, CATALOG

with tempfile.TemporaryDirectory() as folder:
    dxf=Path(folder)/'sample.dxf'
    doc=ezdxf.new('R2010');msp=doc.modelspace()
    frame='--■ DXF_공통_도면구분';doc.layers.new(frame)
    block=doc.blocks.new('SAMPLE_WINDOW')
    block.add_line((0,0),(1000,1000))
    for number,x in enumerate((0,20000),start=1):
        msp.add_lwpolyline([(x,0),(x+12000,0),(x+12000,9000),(x,9000)],close=True,dxfattribs={'layer':frame})
        msp.add_text(f'{number}F 평면도',dxfattribs={'insert':(x+100,8000),'layer':frame})
        msp.add_line((x+100,100),(x+500,100))
        msp.add_blockref('SAMPLE_WINDOW',(x+2000,3000))
    doc.saveas(dxf)
    payload,stats=import_dxf(dxf,dxf.name)
    assert set(payload['floors'])=={'1F','2F'},stats
    assert stats['segments']>=4,stats
    CATALOG.set_direct(payload,dxf.name)
    for floor in ('1F','2F'):
        s=CATALOG.prepare_scene('direct',floor)
        assert s['totalLines']>0
        assert len(s['bin'])==s['totalLines']*20
        assert len(s['texts'])==1
    doc=ezdxf.new('R2010');msp=doc.modelspace();msp.add_line((0,0),(100,100));doc.saveas(dxf)
    payload,stats=import_dxf(dxf,dxf.name)
    assert '전체' in payload['floors'],stats
    print('PASS: DXF read, blocks, title-to-floor, full-drawing fallback, binary payload')
