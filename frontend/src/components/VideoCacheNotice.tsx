import type { VideoCacheState } from "../lib/wholeVideoCache";

type Props = VideoCacheState & { label?: string; pauseReason?: string; manualPaused: boolean; activePlayback: boolean;
  usingLocal: boolean; togglePause: () => void; clear: () => void; retry: () => void };
const size = (bytes: number) => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`;

export default function VideoCacheNotice(p: Props) {
  const label = p.label ?? "完整视频";
  const completed = p.status === "complete";
  const pending = ["checking", "downloading", "paused"].includes(p.status);
  return <div className="video-cache-notice" title="缓存原始播放文件，不降低画质；离开工作台后释放本次临时缓存" data-cache-status={p.status} data-cache-loaded={p.loaded} data-cache-total={p.total}>
    <span className="small" role="status" aria-live="polite">
      {completed ? p.usingLocal ? `${label}已缓存，播放使用本地视频` : `${label}已缓存，暂停后启用`
        : p.status === "error" ? p.error
          : p.status === "cleared" ? "视频缓存已清除，使用在线播放"
            : p.status === "checking" ? "正在检查视频缓存空间…"
              : p.status === "paused" ? p.manualPaused ? "已暂停缓存" : p.pauseReason === "playback" ? "播放期间暂停整段缓存" : p.pauseReason === "frame" ? "优先加载当前帧，随后继续缓存" : p.pauseReason === "hidden" ? "页面回到前台后继续缓存" : "正在读取缓存索引"
                : `正在后台缓存${label}`}
      {p.total > 0 && p.status !== "cleared" && ` · ${Math.floor(p.loaded / p.total * 100)}%（${size(p.loaded)} / ${size(p.total)}）`}
    </span>
    {pending && <button className="btn small" onClick={p.togglePause}>{p.manualPaused ? "继续缓存" : "暂停缓存"}</button>}
    {(p.status === "error" || p.status === "cleared") && <button className="btn small" onClick={p.retry}>重新缓存</button>}
    {p.status !== "cleared" && <button className="btn ghost small" onClick={p.clear} disabled={p.activePlayback}
      title={p.activePlayback ? "请先暂停播放再清除缓存" : "清除本次视频缓存"}>清除缓存</button>}
  </div>;
}
