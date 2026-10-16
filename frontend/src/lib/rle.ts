import type { RLE } from "./types";

/** 把未压缩 COCO RLE（列优先）画到 ImageData 上。 */
export function paintRle(img: ImageData, rle: RLE, rgba: [number, number, number, number]) {
  const [h, w] = rle.size;
  if (img.width !== w || img.height !== h) return;
  const d = img.data;
  let pos = 0;
  let val = false;
  for (const c of rle.counts) {
    if (val) {
      for (let k = pos; k < pos + c; k++) {
        const x = Math.floor(k / h);
        const y = k - x * h;
        const o = (y * w + x) * 4;
        d[o] = rgba[0];
        d[o + 1] = rgba[1];
        d[o + 2] = rgba[2];
        d[o + 3] = rgba[3];
      }
    }
    pos += c;
    val = !val;
  }
}

export function hexToRgb(hex: string): [number, number, number] {
  const n = parseInt(hex.replace("#", ""), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
