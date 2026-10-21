"""Playback subtasks. Canonical annotations and SAM always keep global coordinates."""
import bisect
import hashlib
import json
import math
import logging
import time
import os
import subprocess
import threading
import uuid

from . import db, frame_cache, media

SCHEMA = """
CREATE TABLE IF NOT EXISTS video_segment_plans (
 case_id TEXT PRIMARY KEY REFERENCES cases(case_id) ON DELETE CASCADE,
 seconds INTEGER NOT NULL, context_seconds INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assignment_segment_progress (
 assignment_id INTEGER REFERENCES assignments(id) ON DELETE CASCADE,
 revision TEXT NOT NULL, segment_idx INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 completed_at REAL NOT NULL, PRIMARY KEY(assignment_id,revision,segment_idx)
);
CREATE TABLE IF NOT EXISTS video_segment_jobs (
 case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
 plan_revision TEXT NOT NULL, segment_idx INTEGER NOT NULL, segment_revision TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','processing','ready','error','obsolete')),
 error TEXT, updated_at REAL NOT NULL,
 PRIMARY KEY(case_id,plan_revision,segment_idx)
);
"""
_log = logging.getLogger(__name__)
_service_lock = threading.Lock()
_worker = None
_stop = threading.Event()
_wake = threading.Event()


class PreparationCancelled(RuntimeError):
    pass


def plan(c):
    if c.get('deleted_at') is not None or c['media_status'] != 'ready' or not c.get('media_revision'):
        return []
    conn = db.connect()
    row = conn.execute('SELECT * FROM video_segment_plans WHERE case_id=?', (c['case_id'],)).fetchone()
    if row is None:
        seconds = max(1, int(os.getenv('SSI_SEGMENT_SECONDS', '300')))
        context = max(0, min(30, int(os.getenv('SSI_SEGMENT_CONTEXT_SECONDS', '10'))))
        conn.execute('INSERT OR IGNORE INTO video_segment_plans VALUES (?,?,?)', (c['case_id'], seconds, context))
        row = conn.execute('SELECT * FROM video_segment_plans WHERE case_id=?', (c['case_id'],)).fetchone()
    times = media.frame_times(c['case_id'])
    if not times:
        return []
    duration = max(times[-1] + 1, round(c['video_duration_sec'] * 1000))
    length, context = row['seconds'] * 1000, row['context_seconds'] * 1000
    out = []
    for idx in range(math.ceil(duration / length)):
        start, end = idx * length, min(duration, (idx + 1) * length)
        first, stop = bisect.bisect_left(times, start), bisect.bisect_left(times, end)
        if first == stop:
            continue
        clip_first = bisect.bisect_left(times, max(0, start - context))
        clip_stop = bisect.bisect_left(times, min(duration, end + context))
        revision = hashlib.sha256(f"segment-v1:{c['media_revision']}:{clip_first}:{clip_stop}".encode()).hexdigest()[:32]
        out.append(dict(index=idx, start_ms=start, end_ms=end, first_frame=first, last_frame=stop-1,
                        clip_first_frame=clip_first, clip_last_frame=clip_stop-1,
                        context_start_ms=times[clip_first], context_end_ms=min(duration, end+context), revision=revision))
    return out


def scope(data, s):
    """Canonical subset, including inherited state and events crossing a boundary."""
    lo, hi = s['start_ms'], s['end_ms']
    result = {}
    for key in ('context', 'evaluable'):
        parts = data.get(key, [])
        prior = [p for p in parts if p['start_ms'] <= lo]
        candidates = ([dict(prior[-1], start_ms=lo)] if prior else []) + [p for p in parts if lo < p['start_ms'] < hi]
        result[key] = []
        for part in candidates:
            if not result[key] or result[key][-1]['label'] != part['label']:
                result[key].append(part)
    result['samples'] = {k:v for k,v in data.get('samples', {}).items() if lo <= int(k) < hi}
    result['events'] = [e for e in data.get('events', []) if (e.get('onset_ms') or 0) < hi and
                        (e['recovery_ms'] if e.get('recovery_ms') is not None else float('inf')) >= lo]
    result['milestones'] = {k:v for k,v in data.get('milestones', {}).items() if v.get('time_ms') is None or lo <= v['time_ms'] < hi}
    return result


def fingerprint(a, s):
    value = scope(json.loads(a['data']), s)
    # Mask changes also invalidate completion, without including large RLE payloads in HTTP responses.
    conn = db.connect()
    value['objects'] = [tuple(r) for r in conn.execute('SELECT id,object_id,class,start_ms,end_ms,verified,frame_stride,note FROM mask_objects WHERE assignment_id=? AND start_ms<? AND end_ms>=? ORDER BY id', (a['id'],s['end_ms'],s['start_ms']))]
    value['masks'] = [tuple(r) for r in conn.execute('''SELECT f.object_pk,f.frame_idx,f.updated_at,f.source FROM mask_frames f
        JOIN mask_objects o ON o.id=f.object_pk WHERE o.assignment_id=? AND f.frame_idx BETWEEN ? AND ?
        ORDER BY f.object_pk,f.frame_idx''', (a['id'],s['first_frame'],s['last_frame']))]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def progress(c, a):
    rows = {r['segment_idx']:dict(r) for r in db.connect().execute('SELECT * FROM assignment_segment_progress WHERE assignment_id=? AND revision=?', (a['id'],c['media_revision']))}
    result = plan(c)
    for s in result:
        row = rows.get(s['index'])
        s['completed'] = bool(row and row['fingerprint'] == fingerprint(a,s))
        s['completed_at'] = row['completed_at'] if s['completed'] else None
    return result


def paths(c, s):
    root = media.case_video_dir(c['case_id']) / 'segments' / s['revision']
    return root / 'video.mp4', root / 'frames.csv'


def _build(c, s, cancelled=None):
    if cancelled is not None and cancelled.is_set():
        raise PreparationCancelled()
    dst, mapping = paths(c,s)
    dst.parent.mkdir(parents=True, exist_ok=True)
    temporary = dst.with_name(f'{uuid.uuid4().hex}.partial.mp4')
    tempmap = mapping.with_name(f'{uuid.uuid4().hex}.partial.csv')
    try:
        pts = media.frame_pts(c['case_id'])
        first,last = s['clip_first_frame'],s['clip_last_frame']
        threshold = (pts[first-1]+pts[first])/2 if first else pts[0]-0.00001
        command = ['ffmpeg','-nostdin','-y','-v','error','-threads','2','-filter_threads','1',
                   '-copyts','-seek_timestamp','1','-ss',f'{threshold:.9f}','-noaccurate_seek','-noautorotate',
                   '-i',str(media.case_video_dir(c['case_id']) / c['video_file']),
                   '-map','0:v:0','-an','-sn','-dn','-map_metadata','-1','-map_chapters','-1',
                   '-vf',f"select='gte(t,{threshold:.9f})',setpts=PTS-STARTPTS",'-frames:v',str(last-first+1),
                   '-fps_mode','passthrough','-c:v','libx264','-threads','2','-preset','veryfast','-crf','18',
                   '-pix_fmt','yuv420p','-g','15','-keyint_min','15','-sc_threshold','0','-movflags','+faststart',str(temporary)]
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 1800
        try:
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise PreparationCancelled("服务停止，等待下次启动继续生成")
                if time.monotonic() > deadline:
                    raise media.MediaError("子视频生成超时")
                try:
                    _, stderr = proc.communicate(timeout=0.3)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if proc.returncode:
                raise media.MediaError(stderr[-800:])
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
        times = media.build_frame_map(temporary,tempmap)
        expected = [round((p-pts[first])*1000) for p in pts[first:last+1]]
        if len(times) != len(expected) or any(abs(a-b)>1 for a,b in zip(times,expected)):
            raise media.MediaError('子视频帧时间戳校验失败，未发布该片段')
        if frame_cache.revision(c) != c['media_revision']:
            raise media.MediaError('原视频已变更，请刷新后重新生成子视频')
        # Mapping is published first; video existence is the readiness marker.
        tempmap.replace(mapping)
        temporary.replace(dst)
    finally:
        temporary.unlink(missing_ok=True)
        tempmap.unlink(missing_ok=True)


def plan_revision(items):
    return hashlib.sha256(json.dumps([(s['index'], s['revision']) for s in items]).encode()).hexdigest()[:32]


def available(c, s):
    video, mapping = paths(c, s)
    return video.is_file() and mapping.is_file()


def preparation(c):
    """Read status only. Opening a task must never enqueue an encoder."""
    if c['media_status'] != 'ready':
        return dict(status='waiting', total=0, ready=0, failed=0, error=None)
    if not c.get('media_revision'):
        return dict(status='error', total=0, ready=0, failed=0, error='视频文件或时间戳表缺失')
    try:
        items = plan(c)
        if len(items) <= 1:
            return dict(status='ready', total=0, ready=0, failed=0, error=None)
        revision = plan_revision(items)
        rows = {r['segment_idx']:dict(r) for r in db.connect().execute(
            'SELECT * FROM video_segment_jobs WHERE case_id=? AND plan_revision=?', (c['case_id'],revision))}
        ready = sum(available(c,s) for s in items)
        failures = []
        for item in items:
            row = rows.get(item['index'], {})
            if not available(c,item) and row.get('status') in ('error','ready'):
                failures.append(dict(error=row.get('error') or '已准备的子视频文件缺失，请重新准备'))
        processing = any(r['status'] == 'processing' for r in rows.values())
        state = 'ready' if ready == len(items) else 'error' if failures else 'processing' if processing else 'queued'
        return dict(status=state, total=len(items), ready=ready, failed=len(failures),
                    error=failures[0]['error'] if failures else None)
    except (OSError, ValueError, KeyError, TypeError, media.MediaError) as e:
        return dict(status='error', total=0, ready=0, failed=0, error=str(e)[:2000])


def enqueue_case(c, retry=False):
    """Called at import/startup/configuration, never by an annotator's media read."""
    if c.get('deleted_at') is not None:
        return dict(status='waiting',total=0,ready=0,failed=0,error=None)
    items = plan(c)
    revision = plan_revision(items)
    with db.tx() as conn:
        if conn.execute('SELECT deleted_at FROM cases WHERE case_id=?',(c['case_id'],)).fetchone()['deleted_at'] is not None:
            return dict(status='waiting',total=0,ready=0,failed=0,error=None)
        conn.execute("UPDATE video_segment_jobs SET status='obsolete', updated_at=? WHERE case_id=? AND plan_revision!=? AND status IN ('queued','processing')",
                     (db.now(),c['case_id'],revision))
        if len(items) > 1:
            for item in items:
                state = 'ready' if available(c,item) else 'queued'
                conn.execute("INSERT OR IGNORE INTO video_segment_jobs VALUES (?,?,?,?,?,NULL,?)",
                             (c['case_id'],revision,item['index'],item['revision'],state,db.now()))
                if state == 'ready' or retry:
                    conn.execute("UPDATE video_segment_jobs SET status=?,error=NULL,updated_at=? WHERE case_id=? AND plan_revision=? AND segment_idx=? AND status!='processing'",
                                 (state,db.now(),c['case_id'],revision,item['index']))
                else:
                    # Recover deleted/missing files, but retain failed states until an explicit retry.
                    conn.execute("UPDATE video_segment_jobs SET status='queued',updated_at=? WHERE case_id=? AND plan_revision=? AND segment_idx=? AND status='ready'",
                                 (db.now(),c['case_id'],revision,item['index']))
    _wake.set()
    return preparation(c)


def _current_case(case_id):
    c = db.row(db.connect().execute('SELECT * FROM cases WHERE case_id=? AND deleted_at IS NULL',(case_id,)).fetchone())
    if c is not None:
        c['media_revision'] = frame_cache.revision(c)
    return c


def enqueue_existing():
    count = 0
    for row in db.connect().execute("SELECT case_id FROM cases WHERE media_status='ready' AND deleted_at IS NULL").fetchall():
        try:
            c = _current_case(row['case_id'])
            if c and c['media_revision']:
                enqueue_case(c)
                count += 1
        except Exception:
            _log.exception('Cannot queue video segments for %s', row['case_id'])
    return count


class _JobCancellation:
    def __init__(self, job):
        self.job = job

    def is_set(self):
        if _stop.is_set():
            return True
        row = db.connect().execute(
            "SELECT j.status,c.deleted_at FROM video_segment_jobs j JOIN cases c ON c.case_id=j.case_id "
            "WHERE j.case_id=? AND j.plan_revision=? AND j.segment_idx=?",
            (self.job['case_id'],self.job['plan_revision'],self.job['segment_idx'])).fetchone()
        return row is None or row['status'] != 'processing' or row['deleted_at'] is not None


def _work_one():
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM video_segment_jobs WHERE status='queued' ORDER BY updated_at,case_id,segment_idx LIMIT 1").fetchone()
        if row is None:
            return False
        job = dict(row)
        conn.execute("UPDATE video_segment_jobs SET status='processing',error=NULL,updated_at=? WHERE case_id=? AND plan_revision=? AND segment_idx=?",
                     (db.now(),job['case_id'],job['plan_revision'],job['segment_idx']))
    state, error = 'ready', None
    try:
        c = _current_case(job['case_id'])
        items = plan(c) if c else []
        item = next((s for s in items if s['index']==job['segment_idx']),None)
        if item is None or plan_revision(items) != job['plan_revision'] or item['revision'] != job['segment_revision']:
            state = 'obsolete'
        elif not available(c,item):
            _build(c,item,_JobCancellation(job))
            fresh = _current_case(job['case_id'])
            if not fresh or plan_revision(plan(fresh)) != job['plan_revision']:
                state = 'obsolete'
    except PreparationCancelled:
        state = 'queued'
    except Exception as e:
        state, error = 'error', str(e)[:2000]
        _log.exception('Video segment preparation failed for %s/%s',job['case_id'],job['segment_idx'])
    with db.tx() as conn:
        # A concurrent configuration change can already have marked this job obsolete.
        conn.execute("UPDATE video_segment_jobs SET status=?,error=?,updated_at=? WHERE case_id=? AND plan_revision=? AND segment_idx=? AND status='processing'",
                     (state,error,db.now(),job['case_id'],job['plan_revision'],job['segment_idx']))
    return True


def _run_worker():
    try:
        while not _stop.is_set():
            try:
                if _work_one():
                    continue
            except Exception:
                _log.exception('Video preparation queue temporarily unavailable')
            _wake.wait(1)
            _wake.clear()
    finally:
        db.reset_connection()


def start_preparation_service():
    global _worker
    with _service_lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop.clear()
        db.connect().execute("UPDATE video_segment_jobs SET status='queued',error=NULL WHERE status='processing'")
        enqueue_existing()
        _worker = threading.Thread(target=_run_worker,name='video-preparation',daemon=True)
        _worker.start()


def stop_preparation_service():
    global _worker
    with _service_lock:
        _stop.set()
        _wake.set()
        if _worker is not None:
            _worker.join(timeout=10)
            if _worker.is_alive():
                raise RuntimeError('子视频准备线程未停止')
            _worker = None


def metadata(c, s):
    """Return prebuilt local timestamps, without creating jobs or running FFmpeg."""
    if available(c,s):
        _,mapping = paths(c,s)
        stat = mapping.stat()
        return dict(status='ready',frame_times=list(media._load_frame_times(str(mapping), stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino)))
    row = db.connect().execute('SELECT status,error FROM video_segment_jobs WHERE case_id=? AND plan_revision=? AND segment_idx=?',
                               (c['case_id'],plan_revision(plan(c)),s['index'])).fetchone()
    if row is not None and row['status'] in ('error','ready'):
        return dict(status='error',error=row['error'] or '已准备的子视频文件缺失，请联系管理员重新准备')
    return dict(status='processing')
