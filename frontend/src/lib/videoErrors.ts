export function videoErrorMessage(code: number | undefined): string {
  switch (code) {
    case 1: return '视频读取已中断，可从当前帧重试播放。';
    case 2: return '视频传输中断，可从当前帧重试；也可等待本段缓存完成后播放。';
    case 3: return '视频解码失败，可从当前帧重试；若重复失败，请联系管理员检查播放文件。';
    case 4: return '视频请求未能正常加载（可能是传输失败或格式不受支持），可从当前帧重试。';
    default: return '视频加载失败，可从当前帧重试播放。';
  }
}

/** Reload an errored media resource before seeking; play() alone keeps rejecting. */
export function resetFailedVideo(video: HTMLVideoElement) {
  if (video.error) video.load();
}
