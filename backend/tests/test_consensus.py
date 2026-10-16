"""三专家共识的权限、轮次、内容失效和最终数据出口。"""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from ssi_annotator import auth, cases, consensus, db
from ssi_annotator.app import app
from ssi_annotator.protocol import empty_annotation


def login(c, username):
    c.cookies.clear()
    assert c.post('/api/auth/login', json={'username': username, 'password': 'password123'}).status_code == 200


def act(c, aid, action, comment=None, **overrides):
    p = c.get(f'/api/assignments/{aid}').json()
    s = p.get('consensus') or {}
    body = {'action': action, 'comment': comment, 'consensus_round': s.get('round'), 'consensus_revision': s.get('revision')}
    body.update(overrides)
    return c.post(f'/api/assignments/{aid}/action', json=body)


@pytest.fixture()
def workflow(data_dir):
    users = {name: auth.create_user(name, name, role, 'password123') for name, role in (
        ('admin', 'admin'), ('R1', 'reviewer'), ('R2', 'reviewer'), ('R3', 'reviewer'), ('A01', 'annotator'))}
    cases.create_case('C', {})
    with db.tx() as conn:
        conn.execute("UPDATE cases SET video_duration_sec=10, fps=30, width=8, height=8 WHERE case_id='C'")
    c = TestClient(app)
    login(c, 'admin')
    source = c.post('/api/cases/C/assignments', json={'user_id': users['A01'], 'role': 'primary'}).json()['id']
    ann = empty_annotation()
    ann['evaluable'] = [{'start_ms': 0, 'label': 'E1'}]
    ann['context'] = [{'start_ms': 0, 'label': 'C1'}]
    ann['samples'] = {str(t): dict(scs='2', lamina='V', facet='V', LF_window='V', dura='NA', root='NA') for t in (0, 5000)}
    with db.tx() as conn:
        conn.execute('UPDATE assignments SET data=? WHERE id=?', (json.dumps(ann), source))
    aid = c.post('/api/cases/C/consensus', json={'from_assignment_id': source}).json()['id']
    return c, aid, source, ann, users


def submit(c, aid):
    login(c, 'admin')
    r = act(c, aid, 'submit')
    assert r.status_code == 200, r.text
    assert r.json()['consensus']['approved_count'] == 0


def sign_three(c, aid):
    for i, name in enumerate(('R1', 'R2', 'R3'), 1):
        login(c, name)
        r = act(c, aid, 'approve', f'{name} 确认一致')
        assert r.status_code == 200, r.text
        assert r.json()['consensus']['approved_count'] == i
        assert r.json()['assignment']['status'] == ('approved' if i == 3 else 'submitted')


def test_three_distinct_experts_creator_excluded_and_history_export(workflow):
    c, aid, source, ann, users = workflow
    submit(c, aid)
    # 创建者不能自签；普通标注者不能伪造签署。
    assert act(c, aid, 'approve').status_code == 403
    login(c, 'A01')
    assert c.post(f'/api/assignments/{aid}/action', json={'action': 'approve'}).status_code == 403
    for i, name in enumerate(('R1', 'R2', 'R3'), 1):
        login(c, name)
        assert act(c, aid, 'approve', f'{name} 已检查').status_code == 200
        if i < 3:
            assert c.post('/api/cases/C/lock', json={'final_assignment_id': aid}).status_code == 409
        assert act(c, aid, 'approve').status_code == 409
    p = c.get(f'/api/assignments/{aid}').json()
    assert p['consensus']['complete'] and p['consensus']['approved_count'] == 3
    assert len(c.get(f'/api/assignments/{aid}/consensus-reviews').json()) == 3
    login(c, 'admin')
    assert c.post('/api/cases/C/lock', json={'final_assignment_id': aid}).status_code == 200
    z = zipfile.ZipFile(io.BytesIO(c.get('/api/cases/C/export.zip').content))
    manifest = json.loads(z.read('C/export_manifest.json'))
    assert manifest['consensus']['complete']
    assert len(z.read('C/qc/consensus_reviews.csv').decode().splitlines()) == 4
    login(c, 'R1')
    assert act(c, aid, 'withdraw').status_code == 409
    assert act(c, aid, 'return', '锁定后不可退回').status_code == 409


def test_rejection_resubmit_and_stale_round_cannot_be_reused(workflow):
    c, aid, *_ = workflow
    submit(c, aid)
    login(c, 'R1')
    assert act(c, aid, 'approve').status_code == 200
    old = c.get(f'/api/assignments/{aid}').json()['consensus']
    login(c, 'R2')
    assert act(c, aid, 'return', ' ').status_code == 400
    r = act(c, aid, 'return', '请调整情境边界')
    assert r.status_code == 200 and r.json()['assignment']['status'] == 'returned'
    assert not r.json()['consensus']['complete']
    login(c, 'R3')
    assert act(c, aid, 'approve').status_code == 409
    submit(c, aid)
    login(c, 'R1')
    assert act(c, aid, 'approve', consensus_round=old['round'], consensus_revision=old['revision']).status_code == 409
    sign_three(c, aid)
    history = c.get(f'/api/assignments/{aid}/consensus-reviews').json()
    assert len(history) == 5 and {r['round'] for r in history} == {1, 2}
    assert history[1]['comment'] == '请调整情境边界'


def test_reopen_and_data_edit_invalidate_confirmation(workflow):
    c, aid, *_ = workflow
    submit(c, aid)
    sign_three(c, aid)
    login(c, 'R1')
    assert act(c, aid, 'reopen').status_code == 403
    login(c, 'admin')
    p = c.get(f'/api/assignments/{aid}').json()
    assert not p['editable']
    assert c.put(f'/api/assignments/{aid}/data', json={'data': p['data'], 'version': p['assignment']['version']}).status_code == 403
    assert act(c, aid, 'reopen').status_code == 200
    p = c.get(f'/api/assignments/{aid}').json()
    assert p['editable'] and p['consensus']['approved_count'] == 0
    p['data']['samples']['0']['note'] = '重新核查'
    assert c.put(f'/api/assignments/{aid}/data', json={'data': p['data'], 'version': p['assignment']['version']}).status_code == 200
    assert c.post('/api/cases/C/lock', json={'final_assignment_id': aid}).status_code == 409
    submit(c, aid)
    sign_three(c, aid)


def test_withdrawal_reduces_quorum_and_can_confirm_again(workflow):
    c, aid, *_ = workflow
    submit(c, aid)
    sign_three(c, aid)
    login(c, 'R1')
    r = act(c, aid, 'withdraw', '需要再核查')
    assert r.status_code == 200
    assert r.json()['consensus']['approved_count'] == 2 and not r.json()['consensus']['complete']
    assert c.post('/api/cases/C/lock', json={'final_assignment_id': aid}).status_code == 409
    assert act(c, aid, 'withdraw').status_code == 409
    assert act(c, aid, 'approve').json()['consensus']['complete']
    assert len(c.get(f'/api/assignments/{aid}/consensus-reviews').json()) == 5


def test_background_mask_write_invalidates_signed_version(workflow):
    c, aid, *_ = workflow
    # 模拟掩膜后台写入；SQL 触发器不能只依赖前端或同步编辑接口。
    with db.tx() as conn:
        pk = conn.execute("INSERT INTO mask_objects (assignment_id,object_id,class,start_ms,end_ms,created_at,updated_at) "
                          "VALUES (?,'O001','lamina',0,9000,0,0)", (aid,)).lastrowid
    submit(c, aid)
    sign_three(c, aid)
    with db.tx() as conn:
        conn.execute("INSERT INTO mask_frames (object_pk,frame_idx,time_ms,rle,area,source,updated_at) "
                     "VALUES (?,0,0,?,1,'sam_propagated',0)", (pk, json.dumps({'size': [8,8], 'counts': [0,1,63]})))
    p = c.get(f'/api/assignments/{aid}').json()
    assert p['assignment']['status'] == 'in_progress' and p['consensus']['approved_count'] == 0
    assert c.post('/api/cases/C/lock', json={'final_assignment_id': aid}).status_code == 409


def test_gold_final_cannot_bypass_consensus_with_approved_primary(workflow):
    c, aid, source, *_ = workflow
    with db.tx() as conn:
        conn.execute("UPDATE cases SET gold_set=1 WHERE case_id='C'")
        conn.execute("UPDATE assignments SET status='approved' WHERE id=?", (source,))
    assert c.post('/api/cases/C/lock', json={'final_assignment_id': source}).status_code == 409
    # 旧数据即使被标记锁定，也不能绕过三人要求输出最终版本。
    with db.tx() as conn:
        conn.execute("UPDATE cases SET status='locked',final_assignment_id=? WHERE case_id='C'", (source,))
    assert c.get('/api/cases/C/export.zip').status_code == 409
    assert c.get('/api/project/export.zip').status_code == 409


def test_review_requires_explicit_current_revision_and_content_complete(workflow):
    c, aid, *_ = workflow
    # 不完整草稿不能提交共识，也不能直接标为 approved。
    with db.tx() as conn:
        conn.execute('UPDATE assignments SET data=? WHERE id=?', (json.dumps(empty_annotation()), aid))
    assert act(c, aid, 'submit').status_code == 409
    login(c, 'R1')
    assert act(c, aid, 'approve').status_code == 409


def test_concurrent_duplicate_signature_counts_once(workflow):
    from concurrent.futures import ThreadPoolExecutor
    c, aid, *_ = workflow
    submit(c, aid)
    login(c, 'R1')
    s = c.get(f'/api/assignments/{aid}').json()['consensus']
    body = {'action': 'approve', 'consensus_round': s['round'], 'consensus_revision': s['revision']}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: c.post(f'/api/assignments/{aid}/action', json=body), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert c.get(f'/api/assignments/{aid}').json()['consensus']['approved_count'] == 1


@pytest.mark.parametrize('operation', ['object_update', 'object_delete', 'frame_update', 'frame_delete'])
def test_every_mask_change_invalidates_quorum(workflow, operation):
    c, aid, *_ = workflow
    with db.tx() as conn:
        pk = conn.execute("INSERT INTO mask_objects (assignment_id,object_id,class,start_ms,end_ms,created_at,updated_at) "
                          "VALUES (?,'O001','lamina',0,9000,0,0)", (aid,)).lastrowid
        conn.execute("INSERT INTO mask_frames (object_pk,frame_idx,time_ms,rle,area,source,updated_at) "
                     "VALUES (?,0,0,?,1,'manual_edit',0)", (pk, json.dumps({'size': [8,8], 'counts': [0,1,63]})))
    submit(c, aid)
    sign_three(c, aid)
    statements = {
        'object_update': "UPDATE mask_objects SET verified=1,verified_by=1 WHERE id=?",
        'object_delete': 'DELETE FROM mask_objects WHERE id=?',
        'frame_update': 'UPDATE mask_frames SET area=2 WHERE object_pk=?',
        'frame_delete': 'DELETE FROM mask_frames WHERE object_pk=?',
    }
    with db.tx() as conn:
        conn.execute(statements[operation], (pk,))
    p = c.get(f'/api/assignments/{aid}').json()
    assert p['assignment']['status'] == 'in_progress'
    assert p['consensus']['approved_count'] == 0 and not p['consensus']['complete']


def test_existing_database_upgrade_preserves_annotations_without_inventing_signatures(tmp_path, monkeypatch):
    monkeypatch.setenv('SSI_DATA_DIR', str(tmp_path / 'legacy'))
    db.reset_connection()
    try:
        db.connect().executescript(db.SCHEMA)  # 旧数据库，没有共识签署表与触发器。
        uid = auth.create_user('old_admin', '旧管理员', 'admin', 'password123')
        cases.create_case('OLD', {})
        data = json.dumps(empty_annotation())
        with db.tx() as conn:
            aid = conn.execute("INSERT INTO assignments (case_id,user_id,role,status,data,created_at,updated_at) "
                               "VALUES ('OLD',?,'consensus','approved',?,0,0)", (uid, data)).lastrowid
        db.init_db()
        db.init_db()  # 可重复执行。
        a = dict(db.connect().execute('SELECT * FROM assignments WHERE id=?', (aid,)).fetchone())
        assert a['data'] == data and a['status'] == 'approved'
        assert consensus.summary(a)['approved_count'] == 0 and not consensus.summary(a)['complete']
        assert db.connect().execute('SELECT COUNT(*) FROM consensus_reviews').fetchone()[0] == 0
    finally:
        db.reset_connection()


def test_unlock_invalidates_signatures_before_new_rule_snapshot(workflow):
    c, aid, *_ = workflow
    submit(c, aid)
    sign_three(c, aid)
    login(c, 'admin')
    first = c.post('/api/cases/C/lock', json={'final_assignment_id': aid}).json()
    assert first['locked_rule_snapshot_id']
    assert c.post('/api/cases/C/unlock', json={'comment': '按新规则重新复核'}).status_code == 200
    payload = c.get(f'/api/assignments/{aid}').json()
    assert payload['assignment']['status'] == 'in_progress'
    assert payload['consensus']['approved_count'] == 0 and not payload['consensus']['complete']
    assert c.post('/api/cases/C/lock', json={'final_assignment_id': aid}).status_code == 409
    assert c.put('/api/settings', json={'values': {'sampling_interval_ms': 10000}}).status_code == 200
    submit(c, aid)
    sign_three(c, aid)
    second = c.post('/api/cases/C/lock', json={'final_assignment_id': aid})
    assert second.status_code == 200, second.text
    assert second.json()['locked_rule_snapshot_id'] != first['locked_rule_snapshot_id']
    assert db.connect().execute('SELECT COUNT(*) FROM case_rule_snapshots').fetchone()[0] == 2
    assert len(c.get(f'/api/assignments/{aid}/consensus-reviews').json()) == 6
