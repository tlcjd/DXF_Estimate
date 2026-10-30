"""실행: python web_trial/test_smoke.py. Windows CAD 없이 캐시 연결부만 확인."""
from __future__ import annotations
import pickle
import struct
import tempfile
from pathlib import Path
from server import Catalog

def main():
    with tempfile.TemporaryDirectory() as tmp:
        testdir=Path(tmp)/'사용자파일/프로젝트/임시프로젝트/캐쉬/캐드배경/_source_geometry'
        testdir.mkdir(parents=True)
        file=testdir/'smoke.pkl'
        payload={'version':10,'source_path':'C:/smoke.dwg','floors':{'B3F':{
            'crop_rect':(100,200,10100,10200),
            'segments':[(100,200,1100,2200,255,150,40,2,'ABC',{'layer':'A-WALL','entity_type':'LINE'})],
            'annotation_segments':[],
            'texts':[{'text':'B3F','x':150,'y':220,'height':250,'rgb':(200,200,200),'layer':'TEXT'}],
            'fills':[]}}}
        with file.open('wb') as fp:pickle.dump(payload,fp)
        app=Catalog();result=app.scan(tmp)
        assert len(result['files'])==1,result
        invalid=testdir/'zz_invalid.pkl'
        with invalid.open('wb') as fp:pickle.dump({'not_a_source_cache':True, 'payload':'x'*200},fp)
        result=app.scan(tmp)
        assert len(result['files'])==2,result
        suggestion=app.recommended()
        assert suggestion['id']=='0',suggestion
        floors=app.floors(suggestion['id']);assert floors['floors'][0]['name']=='B3F',floors
        sc=app.prepare_scene('0','B3F')
        assert sc['totalLines']==1,sc['totalLines']
        assert len(sc['bin'])==20
        line=struct.unpack('<4f4B',sc['bin'])
        assert line==(0.,0.,1000.,2000.,255,150,40,0),line
        assert sc['layers'][0]['count']==1
        assert len(sc['texts'])==1
        print('PASS: 기존 벡터 캐시 탐색/층 목록/바이너리 변환/레이어/문자')

if __name__=='__main__':main()
