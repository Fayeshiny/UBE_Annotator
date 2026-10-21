/** Keep only the latest requested frame while a browser seek is in flight. */
export class VideoSeekQueue {
  private pending: number | null = null;

  request(video: HTMLVideoElement, seconds: number) {
    this.pending = seconds;
    this.flush(video);
  }

  flush(video: HTMLVideoElement): boolean {
    if (this.pending === null) return false;
    // Setting currentTime before metadata arrives may be discarded by the browser.
    if (video.readyState === 0 || video.seeking) return true;
    const target = this.pending;
    this.pending = null;
    video.currentTime = target;
    return video.seeking;
  }

  reset() {
    this.pending = null;
  }
}
