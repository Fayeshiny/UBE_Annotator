"""管理员删除用户：即时注销、历史署名保留、账号不可复活。"""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from ssi_annotator import auth, cases, db
from ssi_annotator.app import app
from ssi_annotator.protocol import empty_annotation


def login(c, username):
    c.cookies.clear()
    assert c.post('/api/auth/login', json={'username': username, 'password': 'password123'}).status_code == 200


@pytest.fixture()
def users(data_dir):
    ids = {name: auth.create_user(name, name, role, 'password123') for name, role in (
        ('admin', 'admin'), ('A01', 'annotator'), ('victim', 'reviewer'))}
    c = TestClient(app)
    login(c, 'admin')
    return c, ids


def test_delete_requires_admin_and_cannot_delete_self(users):
    c, ids = users
    c.cookies.clear()
    assert c.delete(f"/api/users/{ids['victim']}").status_code == 401
    for name in ('victim', 'A01'):
        login(c, name)
        assert c.delete(f"/api/users/{ids['admin']}").status_code == 403
    login(c, 'admin')
    assert c.delete(f"/api/users/{ids['admin']}").status_code == 409
    assert c.delete('/api/users/999999').status_code == 404
    assert c.get('/api/auth/me').status_code == 200


def test_unused_user_deleted_session_revoked_and_id_not_reused(users):
    c, ids = users
    victim = TestClient(app)
    login(victim, 'victim')
    r = c.delete(f"/api/users/{ids['victim']}")
    assert r.status_code == 200 and r.json()['mode'] == 'deleted'
    assert db.connect().execute('SELECT 1 FROM users WHERE id=?', (ids['victim'],)).fetchone() is None
    assert victim.get('/api/auth/me').status_code == 401
    assert victim.post('/api/auth/login', json={'username': 'victim', 'password': 'password123'}).status_code == 401
    assert c.delete(f"/api/users/{ids['victim']}").status_code == 404
    log = db.connect().execute('SELECT * FROM user_deletion_log').fetchone()
    assert log['username'] == 'victim' and log['actor_id'] == ids['admin'] and log['deletion_mode'] == 'deleted'
    # 无历史署名的用户名可重建，但编号不能复用。
    r = c.post('/api/users', json={'username': 'victim', 'display_name': '新用户', 'role': 'annotator', 'password': 'password123'})
    assert r.status_code == 200 and r.json()['id'] > ids['victim']
    assert c.delete(f"/api/users/{ids['victim']}").status_code == 404
    assert victim.get('/api/auth/me').status_code == 401


def test_annotator_with_assignment_archived_and_cannot_reactivate(users):
    c, ids = users
    cases.create_case('C', {})
    aid = c.post('/api/cases/C/assignments', json={'user_id': ids['A01'], 'role': 'primary'}).json()['id']
    old_data = db.connect().execute('SELECT data FROM assignments WHERE id=?', (aid,)).fetchone()[0]
    victim = TestClient(app)
    login(victim, 'A01')
    r = c.delete(f"/api/users/{ids['A01']}")
    assert r.status_code == 200 and r.json()['mode'] == 'archived'
    stored = db.connect().execute('SELECT * FROM users WHERE id=?', (ids['A01'],)).fetchone()
    assert stored['active'] == 0 and stored['deleted_at'] is not None and stored['password_hash'] == ''
    assert victim.get('/api/auth/me').status_code == 401
    assert all(u['id'] != ids['A01'] for u in c.get('/api/users').json())
    assert c.get(f'/api/assignments/{aid}').json()['assignment']['owner']['username'] == 'A01'
    assert db.connect().execute('SELECT data FROM assignments WHERE id=?', (aid,)).fetchone()[0] == old_data
    assert c.patch(f"/api/users/{ids['A01']}", json={'active': True}).status_code == 404
    assert c.patch(f"/api/users/{ids['A01']}", json={'password': 'password456'}).status_code == 404
    assert c.post('/api/cases/C/assignments', json={'user_id': ids['A01'], 'role': 'secondary'}).status_code == 404
    assert c.post('/api/users', json={'username': 'A01', 'display_name': '重建', 'role': 'annotator', 'password': 'password123'}).status_code == 409


@pytest.mark.parametrize('reference', ['assignment_history', 'review_log', 'verified_by', 'updated_by', 'mask_history', 'deletion_actor'])
def test_history_only_references_also_preserve_identity(users, reference):
    c, ids = users
    cases.create_case('C', {})
    aid = c.post('/api/cases/C/assignments', json={'user_id': ids['admin'], 'role': 'primary'}).json()['id']
    with db.tx() as conn:
        if reference == 'assignment_history':
            conn.execute("INSERT INTO assignment_history (assignment_id,version,data,user_id,saved_at) VALUES (?,1,'{}',?,0)", (aid, ids['victim']))
        elif reference == 'review_log':
            conn.execute("INSERT INTO review_log (case_id,assignment_id,user_id,action,created_at) VALUES ('C',?,?,'approve',0)", (aid, ids['victim']))
        elif reference == 'mask_history':
            conn.execute("INSERT INTO mask_history (object_pk,action,user_id,created_at) VALUES (999,'delete',?,0)", (ids['victim'],))
        elif reference == 'deletion_actor':
            conn.execute("INSERT INTO user_deletion_log (actor_id,target_user_id,username,display_name,deletion_mode,created_at) "
                         "VALUES (?,999,'old','old','deleted',0)", (ids['victim'],))
        else:
            pk = conn.execute("INSERT INTO mask_objects (assignment_id,object_id,class,start_ms,end_ms,verified_by,created_at,updated_at) "
                              "VALUES (?,'O001','lamina',0,1,?,0,0)", (aid, ids['victim'] if reference == 'verified_by' else None)).lastrowid
            if reference == 'updated_by':
                conn.execute("INSERT INTO mask_frames (object_pk,frame_idx,time_ms,rle,area,source,updated_by,updated_at) "
                             "VALUES (?,0,0,'{}',0,'manual_edit',?,0)", (pk, ids['victim']))
    r = c.delete(f"/api/users/{ids['victim']}")
    assert r.status_code == 200 and r.json()['mode'] == 'archived'
    assert db.connect().execute('SELECT username FROM users WHERE id=?', (ids['victim'],)).fetchone()[0] == 'victim'


def test_deleting_consensus_signer_preserves_signature_and_export(users):
    c, ids = users
    for name in ('R2', 'R3'):
        auth.create_user(name, name, 'reviewer', 'password123')
    cases.create_case('C', {})
    with db.tx() as conn:
        conn.execute("UPDATE cases SET video_duration_sec=5,fps=30 WHERE case_id='C'")
    source = c.post('/api/cases/C/assignments', json={'user_id': ids['A01'], 'role': 'primary'}).json()['id']
    ann = empty_annotation()
    ann['evaluable'] = [{'start_ms': 0, 'label': 'E1'}]
    ann['context'] = [{'start_ms': 0, 'label': 'C1'}]
    ann['samples'] = {'0': dict(scs='2', lamina='V', facet='V', LF_window='V', dura='NA', root='NA')}
    with db.tx() as conn:
        conn.execute('UPDATE assignments SET data=? WHERE id=?', (json.dumps(ann), source))
    aid = c.post('/api/cases/C/consensus', json={'from_assignment_id': source}).json()['id']
    assert c.post(f'/api/assignments/{aid}/action', json={'action': 'submit'}).status_code == 200
    for name in ('victim', 'R2', 'R3'):
        login(c, name)
        s = c.get(f'/api/assignments/{aid}').json()['consensus']
        assert c.post(f'/api/assignments/{aid}/action', json={'action': 'approve', 'consensus_round': s['round'], 'consensus_revision': s['revision']}).status_code == 200
    login(c, 'admin')
    assert c.delete(f"/api/users/{ids['victim']}").json()['mode'] == 'archived'
    assert c.get(f'/api/assignments/{aid}').json()['consensus']['complete']
    assert c.post('/api/cases/C/lock', json={'final_assignment_id': aid}).status_code == 200
    z = zipfile.ZipFile(io.BytesIO(c.get('/api/cases/C/export.zip').content))
    assert 'victim' in z.read('C/qc/consensus_reviews.csv').decode()


def test_last_active_admin_cannot_be_disabled_or_demoted(users):
    c, ids = users
    for body in ({'active': False}, {'role': 'annotator'}):
        assert c.patch(f"/api/users/{ids['admin']}", json=body).status_code == 409
    assert c.get('/api/auth/me').status_code == 200


def test_upgrade_old_users_table_adds_deleted_at(tmp_path, monkeypatch):
    monkeypatch.setenv('SSI_DATA_DIR', str(tmp_path / 'old_users'))
    db.reset_connection()
    try:
        conn = db.connect()
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL, "
                     "role TEXT NOT NULL, password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL)")
        conn.execute("INSERT INTO users (username,display_name,role,password_hash,created_at) VALUES ('old','旧用户','admin','oldhash',0)")
        db.init_db()
        db.init_db()
        u = conn.execute("SELECT * FROM users WHERE username='old'").fetchone()
        assert u['deleted_at'] is None and u['password_hash'] == 'oldhash'
    finally:
        db.reset_connection()


def test_deletion_during_password_check_cannot_create_session(users, monkeypatch):
    c, ids = users
    original = auth.verify_password

    def verify_then_delete(password, stored):
        valid = original(password, stored)
        assert c.delete(f"/api/users/{ids['victim']}").status_code == 200
        return valid

    monkeypatch.setattr(auth, 'verify_password', verify_then_delete)
    assert auth.login('victim', 'password123') is None
    assert db.connect().execute('SELECT COUNT(*) FROM sessions WHERE user_id=?', (ids['victim'],)).fetchone()[0] == 0


def test_role_change_updates_existing_session_and_preserves_own_review_rule(users):
    c, ids = users
    cases.create_case('ROLES', {})
    aid = c.post('/api/cases/ROLES/assignments', json={'user_id': ids['A01'], 'role': 'primary'}).json()['id']
    before = dict(db.connect().execute('SELECT * FROM assignments WHERE id=?', (aid,)).fetchone())
    annotator = TestClient(app)
    login(annotator, 'A01')
    assert annotator.get('/api/project/stats').status_code == 403
    assert c.patch(f"/api/users/{ids['A01']}", json={'role': 'reviewer'}).status_code == 200
    # 已登录用户的后端权限立即按最新角色判断，无需重建账号或会话。
    assert annotator.get('/api/auth/me').json()['role'] == 'reviewer'
    assert annotator.get('/api/project/stats').status_code == 200
    assert annotator.post(f'/api/assignments/{aid}/action', json={'action': 'approve'}).status_code == 403
    after = dict(db.connect().execute('SELECT * FROM assignments WHERE id=?', (aid,)).fetchone())
    assert after == before
    assert c.patch(f"/api/users/{ids['A01']}", json={'role': 'annotator'}).status_code == 200
    assert annotator.get('/api/project/stats').status_code == 403


def test_role_change_only_admin_allowed_and_invalid_role_rejected(users):
    c, ids = users
    for name in ('victim', 'A01'):
        other = TestClient(app)
        login(other, name)
        assert other.patch(f'/api/users/{ids[name]}', json={'role': 'admin'}).status_code == 403
    assert c.patch(f"/api/users/{ids['A01']}", json={'role': 'invalid'}).status_code == 400
    assert c.get('/api/users').json()[0]['role'] == 'annotator'  # A01 未改变
