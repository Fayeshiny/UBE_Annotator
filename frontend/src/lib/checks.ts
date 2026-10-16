import type { Issue } from "./types";

/** 分割操作后仅更新掩膜检查，保留时序标注及其自动保存状态。 */
export function mergeMaskIssues(current: Issue[], refreshed: Issue[]): Issue[] {
  return [...current.filter((i) => i.track !== "masks"), ...refreshed.filter((i) => i.track === "masks")];
}
