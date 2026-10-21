import { useState } from 'react';
import { api } from '../lib/api';
import type { Case } from '../lib/types';

export function preparationReady(c: Case) {
  return c.media_status === 'ready' && (!c.segment_preparation || c.segment_preparation.status === 'ready');
}

export function preparationLabel(c: Case) {
  const p = c.segment_preparation;
  if (!p || p.status === 'ready') return '';
  return `${p.status === 'error' ? '子视频准备失败' : p.status === 'processing' ? '子视频切分中' : '等待服务器切分'} ${p.ready}/${p.total}`;
}

export default function VideoPreparationNotice({ video, canRetry = false, onRetry }: {
  video: Case; canRetry?: boolean; onRetry?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const p = video.segment_preparation;
  async function retry() {
    setBusy(true); setError('');
    try {
      await api.post(`/api/cases/${encodeURIComponent(video.case_id)}/segments/prepare`);
      onRetry?.();
    } catch(e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="page">
    <a className="btn ghost small" href="/">← 返回病例</a>
    <h2>{video.case_id}</h2>
    <div className={p?.status === 'error' ? 'error' : 'notice'} role="status" aria-live="polite">
      <b>{preparationLabel(video)}</b>
      <p>{p?.status === 'error' ? '部分子视频准备失败，已完成片段会保留，请联系管理员重试。' : '服务器正在为该病例预先准备全部子视频。全部就绪后自动开放标注，无需在各子任务中等待切分。'}</p>
      {p?.error && <p>{p.error}</p>}
      {error && <p>{error}</p>}
      {canRetry && p?.status === 'error' && <button className="btn small" disabled={busy} onClick={() => void retry()}>重试失败片段</button>}
    </div>
  </div>;
}
