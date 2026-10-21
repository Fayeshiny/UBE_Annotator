const MiB = 1024 * 1024;

/** Budgets belong to the annotator's browser, independently of server RAM.
 * deviceMemory is a coarse optional browser hint, not currently free memory.
 */
export function frameCachePolicy(deviceMemory?: number) {
  if (typeof deviceMemory === "number" && Number.isFinite(deviceMemory) && deviceMemory > 0) {
    if (deviceMemory >= 8) return {
      decodedBytes: 256 * MiB, decodedEntries: 128,
      compressedBytes: 256 * MiB, compressedEntries: 4096, prefetchFrames: 12,
    };
    if (deviceMemory <= 4) return {
      decodedBytes: 96 * MiB, decodedEntries: 32,
      compressedBytes: 64 * MiB, compressedEntries: 1024, prefetchFrames: 6,
    };
  }
  // Browsers without this hint receive a modest increase, not the largest tier.
  return {
    decodedBytes: 128 * MiB, decodedEntries: 64,
    compressedBytes: 128 * MiB, compressedEntries: 2048, prefetchFrames: 6,
  };
}
