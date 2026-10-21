"""Preparation is import/startup work, never an annotator-entry side effect."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from ssi_annotator import auth,cases,db,media,segments
from ssi_annotator.app import app


def wait_for(cid, state):
    for _ in range(500):
        value=segments.preparation(cases.get_case(cid))
        if value['status']==state: return value
        time.sleep(.02)
    pytest.fail(f'preparation did not reach {state}: {value}')


@pytest.fixture()
def queued(data_dir,numbered_video,monkeypatch):
    monkeypatch.setenv('SSI_SEGMENT_SECONDS','1')
    monkeypatch.setenv('SSI_SEGMENT_CONTEXT_SECONDS','0')
    cases.create_case('PREBUILT',{})
    cases.process_video('PREBUILT',numbered_video,transcode=False)
    yield cases.get_case('PREBUILT')
    segments.stop_preparation_service()


def test_import_queues_all_segments_before_any_assignment_or_visit(queued):
    assert db.connect().execute('SELECT count(*) FROM assignments').fetchone()[0]==0
    state=segments.preparation(queued)
    assert state==dict(status='queued',total=4,ready=0,failed=0,error=None)
    assert db.connect().execute("SELECT count(*) FROM video_segment_jobs WHERE status='queued'").fetchone()[0]==4
    segments.start_preparation_service()
    assert wait_for('PREBUILT','ready')['ready']==4
    assert all(segments.available(queued,s) for s in segments.plan(queued))


def test_annotator_get_and_legacy_post_never_trigger_ffmpeg(queued,monkeypatch):
    owner=auth.create_user('owner','Owner','annotator','password123')
    with db.tx() as conn:
        now=db.now()
        from ssi_annotator.protocol import empty_annotation
        aid=conn.execute('INSERT INTO assignments(case_id,user_id,role,data,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                         ('PREBUILT',owner,'primary',json.dumps(empty_annotation()),now,now)).lastrowid
    client=TestClient(app)
    client.post('/api/auth/login',json={'username':'owner','password':'password123'})
    def forbidden(*args,**kwargs): pytest.fail('read-only annotation entry triggered generation')
    monkeypatch.setattr(segments,'_build',forbidden)
    monkeypatch.setattr(segments,'enqueue_case',forbidden)
    assert client.get(f'/api/assignments/{aid}').json()['case']['segment_preparation']['status']=='queued'
    assert client.get('/api/cases').json()[0]['segment_preparation']['ready']==0
    assert client.get('/api/cases/PREBUILT/segments/0').json()['status']=='processing'
    assert client.post('/api/cases/PREBUILT/segments/0/prepare?retry=true').json()['status']=='processing'
    assert client.post('/api/cases/PREBUILT/segments/prepare').status_code==403


def test_restart_recovers_in_progress_job_and_reuses_completed_files(queued,monkeypatch):
    first=segments.plan(queued)[0]
    segments._build(queued,first)
    video,_=segments.paths(queued,first)
    initial_stat=video.stat().st_mtime_ns
    db.connect().execute("UPDATE video_segment_jobs SET status='processing' WHERE segment_idx=1")
    original=segments._build
    built=[]
    def counted(c,s,cancelled=None):
        built.append(s['index'])
        return original(c,s,cancelled)
    monkeypatch.setattr(segments,'_build',counted)
    segments.start_preparation_service()
    assert wait_for('PREBUILT','ready')['ready']==4
    segments.stop_preparation_service()
    assert sorted(built)==[1,2,3]
    assert video.stat().st_mtime_ns==initial_stat
    segments.start_preparation_service()
    assert wait_for('PREBUILT','ready')['ready']==4
    assert sorted(built)==[1,2,3]


def test_changed_plan_invalidates_queued_work_and_prepares_new_plan(queued):
    old=segments.plan_revision(segments.plan(queued))
    db.connect().execute("UPDATE video_segment_plans SET seconds=2 WHERE case_id='PREBUILT'")
    state=segments.enqueue_case(cases.get_case('PREBUILT'))
    assert state['total']==2 and state['status']=='queued'
    assert db.connect().execute('SELECT count(*) FROM video_segment_jobs WHERE plan_revision=? AND status="obsolete"',(old,)).fetchone()[0]==4
    segments.start_preparation_service()
    assert wait_for('PREBUILT','ready')['ready']==2


def test_interrupted_build_is_requeued_without_publishing_partial_files(queued,monkeypatch):
    original=segments._build
    def cancelled(*args,**kwargs): raise segments.PreparationCancelled()
    monkeypatch.setattr(segments,'_build',cancelled)
    assert segments._work_one()
    assert db.connect().execute("SELECT count(*) FROM video_segment_jobs WHERE status='queued'").fetchone()[0]==4
    assert not any(segments.available(queued,s) for s in segments.plan(queued))
    monkeypatch.setattr(segments,'_build',original)


def test_service_lifespan_prepares_legacy_video_without_import_job(queued):
    db.connect().execute('DELETE FROM video_segment_jobs')
    with TestClient(app) as client:
        assert client.get('/api/health').status_code==200
        assert wait_for('PREBUILT','ready')['ready']==4
    assert segments._worker is None
