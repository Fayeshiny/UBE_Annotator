/** Temporary, disk-backed storage for one full-video download.
 * The owner must revoke any returned Blob URL before disposing this storage.
 */
export interface VideoFileStorage {
  append(blob: Blob): Promise<void>;
  write?(offset: number, blob: Blob): Promise<void>;
  complete(): Promise<Blob>;
  dispose(): Promise<void>;
}

interface StorageBackend {
  append(blob: Blob): Promise<void>;
  write(offset: number, blob: Blob): Promise<void>;
  complete(): Promise<Blob>;
  interrupt(): void;
  dispose(): Promise<void>;
}

const cancelled = () => new DOMException("视频缓存已取消。", "AbortError");
const unavailable = () => new Error("此浏览器无法创建本地视频缓存，请检查浏览器存储权限或使用 HTTPS 访问。");
const randomName = () => `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;

/** Serialize writes while allowing disposal to cancel both active and queued work. */
function ownedStorage(backend: StorageBackend): VideoFileStorage {
  let state: "open" | "completing" | "complete" | "failed" | "disposed" = "open";
  let pending = Promise.resolve();
  let disposal: Promise<void> | undefined;
  let completion: Promise<Blob> | undefined;
  const isDisposed = () => state === "disposed";
  const dispose = (): Promise<void> => {
    if (disposal) return disposal;
    state = "disposed";
    backend.interrupt();
    disposal = pending.then(() => backend.dispose());
    return disposal;
  };
  const enqueue = <T>(operation: () => Promise<T>): Promise<T> => {
    const result = pending.then(async () => {
      if (state === "disposed" || state === "failed") throw cancelled();
      const value = await operation();
      if (isDisposed()) throw cancelled();
      return value;
    });
    pending = result.then(() => {}, () => {});
    // Failed writes must not leave a partial multi-gigabyte cache behind, even
    // when a caller only handles the original download error.
    void result.catch(() => {
      if (state !== "disposed") state = "failed";
      void dispose().catch(() => {});
    });
    return result;
  };
  return {
    append(blob) {
      if (state === "disposed" || state === "failed") return Promise.reject(cancelled());
      if (state !== "open") return Promise.reject(new DOMException("视频缓存已经写入完成。", "InvalidStateError"));
      return enqueue(() => backend.append(blob));
    },
    write(offset, blob) {
      if (state !== "open") return Promise.reject(cancelled());
      if (!Number.isSafeInteger(offset) || offset < 0) return Promise.reject(new RangeError("Invalid video offset"));
      return enqueue(() => backend.write(offset, blob));
    },
    complete() {
      if (state === "disposed" || state === "failed") return Promise.reject(cancelled());
      if (completion) return completion;
      state = "completing";
      completion = enqueue(async () => {
        const blob = await backend.complete();
        if (state !== "disposed") state = "complete";
        return blob;
      });
      return completion;
    },
    dispose,
  };
}

async function opfsStorage(): Promise<StorageBackend> {
  if (typeof navigator === "undefined" || !navigator.storage?.getDirectory) throw unavailable();
  const root = await navigator.storage.getDirectory();
  const directory = await root.getDirectoryHandle("ssi-annotator-video-cache", { create: true });
  const name = `video-${randomName()}.mp4`;
  let stream: FileSystemWritableFileStream | undefined;
  let interrupted: Promise<void> | undefined;
  try {
    const handle = await directory.getFileHandle(name, { create: true });
    stream = await handle.createWritable();
    return {
      async append(blob) {
        if (!stream) throw cancelled();
        await stream.write(blob);
      },
      async write(offset, blob) {
        if (!stream) throw cancelled();
        await stream.write({ type: "write", position: offset, data: blob });
      },
      async complete() {
        if (!stream) throw cancelled();
        const active = stream;
        await active.close();
        if (stream === active) stream = undefined;
        return handle.getFile();
      },
      interrupt() {
        const active = stream;
        stream = undefined;
        if (active) interrupted = active.abort().catch(() => {});
      },
      async dispose() {
        await interrupted;
        try { await directory.removeEntry(name); }
        catch (error) {
          if (!(error instanceof DOMException && error.name === "NotFoundError")) throw error;
        }
      },
    };
  } catch (error) {
    if (stream) await stream.abort().catch(() => {});
    await directory.removeEntry(name).catch(() => {});
    throw error;
  }
}

function deleteDatabase(factory: IDBFactory, name: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = factory.deleteDatabase(name);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error ?? new Error("无法清理本地视频缓存。"));
    request.onblocked = () => reject(new Error("本地视频缓存清理被浏览器阻止。"));
  });
}

async function indexedDbStorage(): Promise<StorageBackend> {
  if (typeof indexedDB === "undefined") throw unavailable();
  const factory = indexedDB;
  // A unique database isolates simultaneous cases/tabs. We only ever delete
  // the database created by this instance, never another tab's active cache.
  const name = `ssi-annotator-video-cache-${randomName()}`;
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    let finished = false;
    const request = factory.open(name, 1);
    request.onupgradeneeded = () => {
      if (finished) request.transaction?.abort();
      else request.result.createObjectStore("chunks");
    };
    request.onsuccess = () => {
      if (finished) {
        request.result.close();
        void deleteDatabase(factory, name).catch(() => {});
      } else {
        finished = true;
        resolve(request.result);
      }
    };
    request.onerror = () => {
      finished = true;
      void deleteDatabase(factory, name).catch(() => {});
      reject(request.error ?? unavailable());
    };
    request.onblocked = () => {
      finished = true;
      reject(new Error("浏览器阻止了本地视频缓存，请检查存储权限。"));
    };
  });
  let nextChunk = 0;
  let disposed = false;
  const transactions = new Set<IDBTransaction>();
  const transaction = <T>(mode: IDBTransactionMode, run: (store: IDBObjectStore) => () => T): Promise<T> =>
    new Promise<T>((resolve, reject) => {
      if (disposed) { reject(cancelled()); return; }
      let tx: IDBTransaction;
      try { tx = db.transaction("chunks", mode); }
      catch (error) { reject(error); return; }
      transactions.add(tx);
      tx.onabort = () => { transactions.delete(tx); reject(tx.error ?? cancelled()); };
      let result: () => T;
      try { result = run(tx.objectStore("chunks")); }
      catch (error) {
        transactions.delete(tx);
        tx.abort();
        reject(error);
        return;
      }
      tx.oncomplete = () => {
        transactions.delete(tx);
        try { resolve(result()); } catch (error) { reject(error); }
      };
    });
  return {
    append(blob) {
      return transaction("readwrite", store => {
        store.put(blob, nextChunk++);
        return () => {};
      });
    },
    write(offset, blob) {
      return transaction("readwrite", store => {
        store.put(blob, offset);
        return () => {};
      });
    },
    complete() {
      return transaction("readonly", store => {
        const parts: Blob[] = [];
        const cursor = store.openCursor();
        cursor.onsuccess = () => {
          const entry = cursor.result;
          if (entry) { parts.push(entry.value as Blob); entry.continue(); }
        };
        // Blob composition keeps browser-managed Blob references. Do not call
        // arrayBuffer()/FileReader: the entire video need not enter JS memory.
        return () => new Blob(parts, { type: "video/mp4" });
      });
    },
    interrupt() {
      disposed = true;
      for (const tx of transactions) {
        try { tx.abort(); } catch { /* An already completed transaction is harmless. */ }
      }
    },
    async dispose() {
      disposed = true;
      db.close();
      await deleteDatabase(factory, name);
    },
  };
}

/** OPFS needs a secure context. IndexedDB also works on remote HTTP origins. */
export async function createVideoFileStorage(): Promise<VideoFileStorage> {
  try { return ownedStorage(await opfsStorage()); }
  catch {
    try { return ownedStorage(await indexedDbStorage()); }
    catch (error) {
      if (error instanceof Error && error.name === "QuotaExceededError") throw error;
      throw unavailable();
    }
  }
}
