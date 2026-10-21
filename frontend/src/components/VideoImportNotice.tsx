import type { Case } from "../lib/types";

export default function VideoImportNotice({ video }: { video: Pick<Case, "case_id" | "media_status" | "media_error"> }) {
  if (video.media_status === "ready") return null;
  const processing = video.media_status === "processing";
  return <div className="page">
    <a className="btn ghost small" href="/">← 返回病例</a>
    <h2>{video.case_id}</h2>
    <div className={processing ? "notice" : "error"} role="status" aria-live="polite">
      <b>{processing ? "视频正在处理，暂时不能播放或标注。" : "视频处理失败，暂时不能播放或标注。"}</b>
      <p>{processing ? "处理完成后会自动加载。长视频可能需要几分钟，请保持后端服务运行。"
        : video.media_error || "请联系管理员检查原始文件并重新处理。"}</p>
      {!processing && <button className="btn small" onClick={() => window.location.reload()}>刷新状态</button>}
    </div>
  </div>;
}
