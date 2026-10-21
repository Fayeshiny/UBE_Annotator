import json
import time

import pytest
from fastapi.testclient import TestClient

from ssi_annotator import auth, cases, db, frame_cache, media, protocol, segments
from ssi_annotator.app import app


@pytest.fixture()
def task(data_dir, numbered_video, monkeypatch):
    monkeypatch.setenv('SSI_SEGMENT_SECONDS','1')
    monkeypatch.setenv('SSI_SEGMENT_CONTEXT_SECONDS','1')
    cases.create_case('SEGMENTS', {})
    cases.process_video('SEGMENTS',numbered_video,transcode=False)
    uid = auth.create_user('owner','Owner','annotator','password123')
    auth.create_user('other','Other','annotator','password123')
    with db.tx() as conn:
        aid = conn.execute('INSERT INTO assignments(case_id,user_id,role,data,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                           ('SEGMENTS',uid,'primary',json.dumps(protocol.empty_annotation()),db.now(),db.now())).lastrowid
    client = TestClient(app)
    client.post('/api/auth/login',json={'username':'owner','password':'password123'})
    segments.start_preparation_service()
    try:
        for _ in range(400):
            if segments.preparation(cases.get_case('SEGMENTS'))['status'] in ('ready','error'): break
            time.sleep(.02)
        assert segments.preparation(cases.get_case('SEGMENTS'))['status']=='ready'
        yield client,aid
    finally:
        segments.stop_preparation_service()
        frame_cache.close_all()


def test_partition_and_physical_clip_exact_global_mapping(task):
    client,aid = task
    p = client.get(f'/api/assignments/{aid}').json()
    ss = p['segments']
    assert [(s['first_frame'],s['last_frame']) for s in ss] == [(0,29),(30,59),(60,89),(90,119)]
    assert (ss[2]['clip_first_frame'],ss[2]['clip_last_frame']) == (30,119)
    assert p['case']['segment_preparation']['status']=='ready'
    assert all(segments.available(cases.get_case('SEGMENTS'), s) for s in ss)
    url = '/api/cases/SEGMENTS/segments/2' 
    assert TestClient(app).post(url+'/prepare').status_code == 401
    client.post('/api/auth/login',json={'username':'other','password':'password123'})
    assert client.post(url+'/prepare').status_code == 403
    client.post('/api/auth/login',json={'username':'owner','password':'password123'})
    for _ in range(200):
        r = client.post(url+'/prepare').json()
        if r['status'] != 'processing': break
        time.sleep(.03)
    assert r['status'] == 'ready',r
    assert len(r['frame_times']) == 90
    c = cases.get_case('SEGMENTS'); clip,mapping = segments.paths(c,ss[2])
    # Decode near both ends: segment frame 0 must be global frame 30, not global frame 0.
    from PIL import Image
    for local,global_idx in [(0,30),(30,60),(89,119)]:
        paths = media.extract_frame_batch(clip, tuple(float(x['pts_sec']) for x in __import__('csv').DictReader(mapping.open())), local,local,1,clip.parent/f'check-{local}')
        import numpy as np
        assert abs(np.asarray(Image.open(clip.parent/f'check-{local}'/'00000.jpg')).mean() - global_idx*2) < 5
    video_url = url+f'/video?revision={ss[2]["revision"]}'
    head = client.head(video_url)
    assert head.status_code == 200 and head.headers['x-media-revision'] == ss[2]['revision']
    ranged = client.get(video_url,headers={'Range':'bytes=0-99'})
    assert ranged.status_code == 206 and ranged.content == clip.read_bytes()[:100]
    assert client.get(url+'/video?revision=old').status_code == 409
    index = client.get(url+'/video-index').json()
    assert index['ranges'][0][0] == 0 and max(x[1] for x in index['ranges']) == 89
    assert client.post('/api/cases/SEGMENTS/segments/999/prepare').status_code == 404


def test_progress_version_and_scoped_invalidation(task):
    client,aid = task
    get = lambda: client.get(f'/api/assignments/{aid}').json()
    p = get()
    url = f'/api/assignments/{aid}/segments/0/completion'
    body = {'completed':True,'version':p['assignment']['version'],'revision':p['case']['media_revision']}
    assert client.post(url,json=body).json()['segments'][0]['completed']
    body['version'] -= 1
    assert client.post(url,json=body).status_code == 409
    # Changes to a different subtask retain this completion.
    p = get(); data = p['data']; data['samples']['2000'] = {'note':'other segment'}
    p = client.put(f'/api/assignments/{aid}/data',json={'version':p['assignment']['version'],'data':data}).json()
    assert p['segments'][0]['completed']
    data['samples']['0'] = {'note':'edited core'}
    p = client.put(f'/api/assignments/{aid}/data',json={'version':p['assignment']['version'],'data':data}).json()
    assert not p['segments'][0]['completed']
    assert client.post(f'/api/assignments/{aid}/action',json={'action':'submit'}).status_code == 409
    assert '子任务' in client.post(f'/api/assignments/{aid}/action',json={'action':'submit'}).text


def test_inherited_state_and_cross_boundary_event_scope():
    data=protocol.empty_annotation()
    data['context']=[{'start_ms':0,'label':'C1'},{'start_ms':1500,'label':'C2'}]
    data['events']=[{'id':'E1','onset_ms':800,'offset_ms':1800,'recovery_ms':2200}]
    value=segments.scope(data,{'start_ms':1000,'end_ms':2000})
    assert value['context'][0] == {'start_ms':1000,'label':'C1'}
    assert value['events'][0]['id']=='E1' and value['events'][0]['onset_ms']==800


def test_admin_configuration_and_full_case_export(task):
    import csv
    import io
    import zipfile
    client,aid = task
    assert client.put('/api/cases/SEGMENTS/segment-plan',json={'seconds':30}).status_code == 403
    auth.create_user('admin','Admin','admin','password123')
    client.post('/api/auth/login',json={'username':'admin','password':'password123'})
    p = client.get(f'/api/assignments/{aid}').json()
    ann = p['data'];ann['samples']={'0':{'note':'first'},'3000':{'note':'last'}}
    ann['events']=[{'id':'E1','onset_ms':900,'offset_ms':2100,'recovery_ms':3100}]
    with db.tx() as conn:
        conn.execute('UPDATE assignments SET data=? WHERE id=?',(json.dumps(ann),aid))
    response=client.get(f'/api/cases/SEGMENTS/export.zip?mode=working&assignment_id={aid}')
    assert response.status_code==200,response.text
    z=zipfile.ZipFile(io.BytesIO(response.content))
    event_rows=list(csv.DictReader(io.StringIO(z.read('SEGMENTS/temporal/orientation_loss.csv').decode('utf-8-sig'))))
    assert len(event_rows)==1 and event_rows[0]['onset_ms']=='900' and event_rows[0]['offset_ms']=='2100'
    assert not any('/segments/' in name for name in z.namelist())
    assert client.put('/api/cases/SEGMENTS/segment-plan',json={'seconds':0}).status_code==422
    configured=client.put('/api/cases/SEGMENTS/segment-plan',json={'seconds':30,'context_seconds':2})
    assert configured.status_code==200 and len(configured.json())==1
    assert client.get(f'/api/assignments/{aid}').json()['data']['events'][0]['id']=='E1'
    with db.tx() as conn: conn.execute("UPDATE assignments SET status='submitted' WHERE id=?",(aid,))
    assert client.put('/api/cases/SEGMENTS/segment-plan',json={'seconds':60}).status_code==409


@pytest.mark.parametrize('offset',[0,7])
def test_variable_rate_clip_timestamp_alignment(data_dir, numbered_video, tmp_path, monkeypatch, offset):
    import subprocess
    monkeypatch.setenv('SSI_SEGMENT_SECONDS','1')
    monkeypatch.setenv('SSI_SEGMENT_CONTEXT_SECONDS','0')
    video=tmp_path/'vfr.mp4'
    subprocess.run(['ffmpeg','-nostdin','-y','-v','error','-i',str(numbered_video),'-vf',f'setpts=(N+floor(N/7))/30/TB+{offset}/TB','-fps_mode','passthrough','-c:v','libx264','-threads','2','-pix_fmt','yuv420p',str(video)],check=True)
    cases.create_case('VFR_SEGMENTS',{})
    cases.process_video('VFR_SEGMENTS',video,transcode=False)
    c=cases.get_case('VFR_SEGMENTS');s=segments.plan(c)[2]
    segments._build(c,s)
    clip,mapping=segments.paths(c,s)
    import csv
    with mapping.open() as f: times=[int(r['time_ms']) for r in csv.DictReader(f)]
    original=media.frame_pts(c['case_id'])[s['first_frame']:s['last_frame']+1]
    assert times==[round((p-original[0])*1000) for p in original]
    assert len(set(b-a for a,b in zip(times,times[1:])))>1


def test_completion_ignores_equivalent_redundant_state_boundaries():
    a=protocol.empty_annotation();a['context']=[{'start_ms':0,'label':'C1'}]
    b=json.loads(json.dumps(a));b['context'].append({'start_ms':1000,'label':'C1'})
    s={'start_ms':1000,'end_ms':2000}
    assert segments.scope(a,s)==segments.scope(b,s)


def test_pending_recovery_remains_visible_in_later_subtasks():
    data=protocol.empty_annotation()
    data['events']=[{'id':'E1','onset_ms':100,'offset_ms':300,'recovery_ms':None}]
    assert segments.scope(data,{'start_ms':1000,'end_ms':2000})['events'][0]['id']=='E1'


def test_failed_generation_exposes_error_and_can_retry(task, monkeypatch):
    client,aid=task
    c=cases.get_case('SEGMENTS');s=segments.plan(c)[1]
    video,mapping=segments.paths(c,s)
    video.unlink();mapping.unlink()
    original=segments._build
    def broken(*_): raise media.MediaError('synthetic generation failure')
    monkeypatch.setattr(segments,'_build',broken)
    segments.enqueue_case(c)
    for _ in range(100):
        state=segments.preparation(cases.get_case('SEGMENTS'))
        if state['status']=='error': break
        time.sleep(.02)
    assert state['status']=='error' and 'synthetic' in state['error']
    assert state['ready']==3 and state['failed']==1
    assert client.head('/api/cases/SEGMENTS/segments/1/video').status_code==409
    # Annotator reads, including the old POST alias, never restart the encoder.
    assert client.post('/api/cases/SEGMENTS/segments/1/prepare?retry=true').json()['status']=='error'
    assert client.post('/api/cases/SEGMENTS/segments/prepare').status_code==403
    monkeypatch.setattr(segments,'_build',original)
    auth.create_user('retry-admin','Admin','admin','password123')
    client.post('/api/auth/login',json={'username':'retry-admin','password':'password123'})
    assert client.post('/api/cases/SEGMENTS/segments/prepare').status_code==200
    for _ in range(200):
        state=segments.preparation(cases.get_case('SEGMENTS'))
        if state['status']=='ready': break
        time.sleep(.02)
    assert state['status']=='ready',state


def test_parent_submit_requires_all_children_and_keeps_global_assignment(task):
    client,aid=task
    p=client.get(f'/api/assignments/{aid}').json()
    data=p['data'];data['context']=[{'start_ms':0,'label':'CU'}];data['evaluable']=[{'start_ms':0,'label':'E0'}]
    p=client.put(f'/api/assignments/{aid}/data',json={'data':data,'version':p['assignment']['version']}).json()
    assert client.post(f'/api/assignments/{aid}/action',json={'action':'submit'}).status_code==409
    for s in p['segments']:
        r=client.post(f'/api/assignments/{aid}/segments/{s["index"]}/completion',json={
            'completed':True,'version':p['assignment']['version'],'revision':p['case']['media_revision']})
        assert r.status_code==200,r.text
    parent=client.get('/api/cases').json()[0]['assignments'][0]
    assert parent['segment_total']==4 and parent['segment_completed']==4
    assert 'data' not in parent
    submitted=client.post(f'/api/assignments/{aid}/action',json={'action':'submit'})
    assert submitted.status_code==200,submitted.text
    assert submitted.json()['assignment']['id']==aid and submitted.json()['assignment']['status']=='submitted'
    assert len(submitted.json()['data']['context'])==1
    assert client.post(f'/api/assignments/{aid}/segments/0/completion',json={
        'completed':False,'version':p['assignment']['version'],'revision':p['case']['media_revision']}).status_code==403
