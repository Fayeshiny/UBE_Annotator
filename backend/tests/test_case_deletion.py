"""Recycle a case without losing videos, annotation history or global identities."""
import io
import json
import threading
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from ssi_annotator import auth,cases,db,media,protocol,segments
from ssi_annotator.app import app


@pytest.fixture()
def fixture(data_dir,numbered_video,monkeypatch):
    monkeypatch.setenv('SSI_SEGMENT_SECONDS','1')
    monkeypatch.setenv('SSI_FRAME_CACHE_PREWARM','0')
    cases.create_case('RECYCLE',{'gold_set':True})
    cases.process_video('RECYCLE',numbered_video,transcode=False)
    for name,role in [('admin','admin'),('reviewer','reviewer'),('owner','annotator')]:
        auth.create_user(name,name,role,'password123')
    owner=db.connect().execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]
    data=protocol.empty_annotation();data['samples']={'0':{'note':'retain this annotation'}}
    with db.tx() as conn:
        now=db.now()
        aid=conn.execute('INSERT INTO assignments(case_id,user_id,role,data,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                         ('RECYCLE',owner,'primary',json.dumps(data),2,now,now)).lastrowid
        conn.execute('INSERT INTO assignment_history(assignment_id,version,data,user_id,saved_at) VALUES (?,?,?,?,?)',(aid,2,json.dumps(data),owner,now))
    def client(name):
        c=TestClient(app);c.post('/api/auth/login',json={'username':name,'password':'password123'});return c
    yield client,aid,data
    segments.stop_preparation_service()


def remove(c,typed='RECYCLE'):
    return c.request('DELETE','/api/cases/RECYCLE',json={'confirm_case_id':typed})


def test_delete_and_restore_preserve_annotation_and_files(fixture):
    login,aid,data=fixture;admin=login('admin')
    case=cases.get_case('RECYCLE');video=media.case_video_dir('RECYCLE')/case['video_file']
    contents=video.read_bytes()
    assert remove(admin).status_code==200
    assert cases.get_case('RECYCLE') is None
    assert admin.get('/api/cases').json()==[]
    assert admin.get('/api/cases/deleted').json()[0]['case_id']=='RECYCLE'
    for url in ['/api/cases/RECYCLE',f'/api/assignments/{aid}',f'/api/assignments/{aid}/history',f'/api/assignments/{aid}/mask-objects',
                '/api/cases/RECYCLE/video','/api/cases/RECYCLE/frames','/api/cases/RECYCLE/frame/0.jpg','/api/cases/RECYCLE/segments/0']:
        assert admin.get(url).status_code in (403,404),url
    assert login('owner').get('/api/cases').json()==[]
    summary=admin.get('/api/project/stats').json()
    assert summary['cases']==0 and sum(summary['assignments'].values())==0
    exported=admin.get('/api/project/export.zip?mode=all')
    assert exported.status_code==200,exported.text
    z=zipfile.ZipFile(io.BytesIO(exported.content))
    assert all('RECYCLE' not in name for name in z.namelist())
    assert 'RECYCLE' not in z.read('cases.csv').decode('utf-8-sig')
    assert video.read_bytes()==contents
    assert admin.post('/api/cases/RECYCLE/restore').status_code==200
    restored=admin.get(f'/api/assignments/{aid}').json()
    assert restored['data']==data and restored['assignment']['version']==2
    assert len(admin.get(f'/api/assignments/{aid}/history').json())==1
    assert admin.get('/api/cases/deleted').json()==[]
    assert video.read_bytes()==contents
    assert db.connect().execute("SELECT action FROM review_log WHERE action IN ('delete_case','restore_case') ORDER BY id").fetchall()[0]['action']=='delete_case'


def test_authorization_confirmation_and_duplicate_id(fixture):
    login,aid,_=fixture
    for role in ['owner','reviewer']:
        assert remove(login(role)).status_code==403
        assert login(role).get('/api/cases/deleted').status_code==403
        assert login(role).post('/api/cases/RECYCLE/restore').status_code==403
    admin=login('admin')
    assert remove(admin,'wrong').status_code==400
    assert cases.get_case('RECYCLE') is not None
    assert remove(admin).status_code==200
    with pytest.raises(ValueError): cases.create_case('RECYCLE',{})
    assert remove(admin).status_code==404
    assert admin.post('/api/cases/RECYCLE/restore').status_code==200
    assert admin.post('/api/cases/RECYCLE/restore').status_code==409


def test_locked_and_importing_cases_must_not_be_deleted(fixture):
    login,_,_=fixture;admin=login('admin')
    db.connect().execute("UPDATE cases SET status='locked' WHERE case_id='RECYCLE'")
    assert remove(admin).status_code==409
    db.connect().execute("UPDATE cases SET status='open',media_status='processing' WHERE case_id='RECYCLE'")
    assert remove(admin).status_code==409
    db.connect().execute("UPDATE cases SET media_status='error' WHERE case_id='RECYCLE'")
    assert remove(admin).status_code==200


def test_deletion_cancels_background_cutting_and_restore_requeues(fixture,monkeypatch):
    login,_,_=fixture;admin=login('admin')
    started=threading.Event();cancelled=threading.Event()
    original=segments._build
    def wait_for_delete(c,s,signal):
        started.set()
        while not signal.is_set(): time.sleep(.01)
        cancelled.set()
        raise segments.PreparationCancelled()
    monkeypatch.setattr(segments,'_build',wait_for_delete)
    segments.start_preparation_service()
    assert started.wait(3)
    assert remove(admin).status_code==200
    assert cancelled.wait(3)
    segments.stop_preparation_service()
    assert db.connect().execute("SELECT count(*) FROM video_segment_jobs WHERE status IN ('queued','processing')").fetchone()[0]==0
    assert segments.enqueue_existing()==0
    monkeypatch.setattr(segments,'_build',original)
    assert admin.post('/api/cases/RECYCLE/restore').status_code==200
    assert db.connect().execute("SELECT count(*) FROM video_segment_jobs WHERE status='queued'").fetchone()[0]==4
