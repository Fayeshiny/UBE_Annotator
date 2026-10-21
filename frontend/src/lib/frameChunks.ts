/** Small frame blocks contain original JPEG bytes, never re-encoded previews. */
export async function readFrameChunk(blob: Blob, revision: string, start: number, count: number,
  exclude: number): Promise<[number, Blob][]> {
  if (blob.size < 4 || blob.size > 8 * 1024 * 1024 + 65540) throw new Error("附近帧块大小不正确");
  const length = new DataView(await blob.slice(0, 4).arrayBuffer()).getUint32(0);
  if (length > 65536 || length + 4 > blob.size) throw new Error("附近帧块索引不完整");
  const metadata = JSON.parse(await blob.slice(4, 4 + length).text());
  if (metadata.revision !== revision || !Array.isArray(metadata.frames)) throw new Error("附近帧块版本不一致");
  let offset = 4 + length;
  const entries: [number, Blob][] = [];
  const seen = new Set<number>();
  for (const item of metadata.frames) {
    if (!Array.isArray(item) || item.length !== 2) throw new Error("附近帧块索引错误");
    const [frame, bytes] = item;
    if (!Number.isSafeInteger(frame) || frame < start || frame >= start + count || frame === exclude || seen.has(frame)
      || !Number.isSafeInteger(bytes) || bytes <= 0 || offset + bytes > blob.size) throw new Error("附近帧块范围错误");
    entries.push([frame, blob.slice(offset, offset + bytes, "image/jpeg")]);
    seen.add(frame);
    offset += bytes;
  }
  if (offset !== blob.size) throw new Error("附近帧块长度不一致");
  return entries;
}

export async function fetchFrameChunk(url: string, revision: string, start: number, count: number,
  exclude: number, signal: AbortSignal, onFrame?: (entry: [number, Blob]) => void): Promise<[number, Blob][]> {
  const response = await fetch(url, { credentials: "same-origin", signal, priority: "low" } as RequestInit);
  if (response.status === 401) window.dispatchEvent(new Event("ssi-unauthorized"));
  if (!response.ok || response.headers.get("X-Media-Revision") !== revision) throw new Error("附近帧块暂不可用");
  if (!response.body) {
    const entries = await readFrameChunk(await response.blob(), revision, start, count, exclude);
    if (signal.aborted) throw new DOMException("Frame chunk cancelled", "AbortError");
    entries.forEach(entry => onFrame?.(entry));
    return entries;
  }
  const reader = response.body.getReader();
  let pending = new Uint8Array(0), cursor = 0, received = 0;
  const current = () => { if (signal.aborted) throw new DOMException("Frame chunk cancelled", "AbortError"); };
  const read = async (bytes: number): Promise<Blob> => {
    const parts: BlobPart[] = [];
    while (bytes) {
      current();
      if (cursor === pending.length) {
        const next = await reader.read();
        current();
        if (next.done) throw new Error("附近帧块不完整");
        received += next.value.length;
        if (received > 8 * 1024 * 1024 + 65540) throw new Error("附近帧块过大");
        pending = next.value;
        cursor = 0;
      }
      const n = Math.min(bytes, pending.length - cursor);
      parts.push(new Blob([pending.subarray(cursor, cursor + n)]));
      cursor += n;
      bytes -= n;
    }
    return new Blob(parts);
  };
  try {
    const length = new DataView(await (await read(4)).arrayBuffer()).getUint32(0);
    if (length > 65536) throw new Error("附近帧块索引过大");
    const metadata = JSON.parse(await (await read(length)).text());
    // Validate every index before exposing even the first complete JPEG.
    if (metadata.revision !== revision || !Array.isArray(metadata.frames)) throw new Error("附近帧块版本不一致");
    const seen = new Set<number>();
    let payloadSize = 0;
    for (const item of metadata.frames) {
      if (!Array.isArray(item) || item.length !== 2) throw new Error("附近帧块索引错误");
      const [frame, bytes] = item;
      if (!Number.isSafeInteger(frame) || frame < start || frame >= start + count || frame === exclude || seen.has(frame)
        || !Number.isSafeInteger(bytes) || bytes <= 0) throw new Error("附近帧块范围错误");
      seen.add(frame);
      payloadSize += bytes;
    }
    if (payloadSize > 8 * 1024 * 1024) throw new Error("附近帧块过大");
    const entries: [number, Blob][] = [];
    for (const [frame, bytes] of metadata.frames) {
      const entry: [number, Blob] = [frame, (await read(bytes)).slice(0, bytes, "image/jpeg")];
      current();
      entries.push(entry);
      onFrame?.(entry);
    }
    if (cursor !== pending.length || !(await reader.read()).done) throw new Error("附近帧块长度不一致");
    current();
    return entries;
  } finally { await reader.cancel().catch(() => {}); }
}
