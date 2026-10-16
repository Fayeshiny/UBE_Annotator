"""时间轴推导、完整性/一致性检查、核心指标与标注者间一致性。

纯函数，不访问数据库，便于单元测试。
"""

import bisect
import math
from collections import Counter

from .protocol import ANCHOR_FIELDS, MILESTONES

SCS_VALUES = ["0", "1", "2"]
VIS_VALUES = ["V", "N", "U", "NA"]


# ---------------------------------------------------------------- 时间轴划分


def normalize_parts(parts: list[dict]) -> list[dict]:
    """排序、去重、保证从 0 开始。"""
    parts = sorted(({"start_ms": int(p["start_ms"]), "label": p.get("label")} for p in parts), key=lambda p: p["start_ms"])
    out: list[dict] = []
    for p in parts:
        if out and out[-1]["start_ms"] == p["start_ms"]:
            out[-1] = p
        else:
            out.append(p)
    if not out or out[0]["start_ms"] != 0:
        out.insert(0, {"start_ms": 0, "label": None})
    return out


def intervals(parts: list[dict], duration_ms: int, merge_same=True) -> list[dict]:
    """把分段起点列表转为 [start_ms, end_ms) 区间；相邻同标签自动合并。"""
    parts = [p for p in normalize_parts(parts) if p["start_ms"] < duration_ms]
    out: list[dict] = []
    for i, p in enumerate(parts):
        end = parts[i + 1]["start_ms"] if i + 1 < len(parts) else duration_ms
        if merge_same and out and out[-1]["label"] == p["label"]:
            out[-1]["end_ms"] = end
        else:
            out.append({"label": p["label"], "start_ms": p["start_ms"], "end_ms": end})
    return out


class Lookup:
    def __init__(self, parts: list[dict]):
        parts = normalize_parts(parts)
        self.starts = [p["start_ms"] for p in parts]
        self.labels = [p["label"] for p in parts]

    def at(self, t: int):
        i = bisect.bisect_right(self.starts, t) - 1
        return self.labels[max(i, 0)]


def sample_times(duration_ms: int, interval_ms: int) -> list[int]:
    return list(range(0, max(duration_ms, 0), interval_ms)) if interval_ms > 0 else []


def duration_ms_of(case: dict) -> int:
    return int(round((case.get("video_duration_sec") or 0) * 1000))


# ---------------------------------------------------------------- 行展开


def sample_rows(case: dict, ann: dict, settings: dict, interval_ms: int | None = None) -> list[dict]:
    """sampled_ssi.csv 的逻辑行（不含 frame_idx / annotator_id）。"""
    dur = duration_ms_of(case)
    interval_ms = interval_ms or settings["sampling_interval_ms"]
    ev = Lookup(ann["evaluable"])
    cx = Lookup(ann["context"])
    rows = []
    for t in sample_times(dur, interval_ms):
        s = ann["samples"].get(str(t), {})
        e = ev.at(t)
        evaluable = 1 if e == "E1" else (0 if e == "E0" else None)
        row = {"time_ms": t, "context": cx.at(t), "evaluable": evaluable}
        row["SCS"] = s.get("scs") if evaluable == 1 else None
        for f in ANCHOR_FIELDS:
            applicable = settings.get("anchor_applicability", {}).get(row["context"])
            value = s.get(f)
            if applicable is not None and f not in applicable and not value:
                value = "NA"
            row[f] = value if evaluable == 1 else None
        row["note"] = s.get("note", "")
        rows.append(row)
    return rows


def event_rows(ann: dict) -> list[dict]:
    cx = Lookup(ann["context"])
    out = []
    for e in sorted(ann["events"], key=lambda e: e["onset_ms"]):
        onset, offset, rec = e.get("onset_ms"), e.get("offset_ms"), e.get("recovery_ms")
        out.append({
            **e,
            "duration_ms": offset - onset if onset is not None and offset is not None else None,
            "SRT_ms": rec - offset if rec is not None and offset is not None else None,
            "context": e.get("context") or (cx.at(onset) if onset is not None else None),
        })
    return out


# ---------------------------------------------------------------- 检查


def _issue(level, track, message, time_ms=None, ref=None, fix=None):
    d = {"level": level, "track": track, "message": message}
    if time_ms is not None:
        d["time_ms"] = time_ms
    if ref is not None:
        d["ref"] = ref
    if fix is not None:
        d["fix"] = fix
    return d


def anchor_issues(ann: dict, settings: dict) -> list[dict]:
    """已填写锚点必须符合当前情境；空值允许保存草稿。"""
    cx = Lookup(ann["context"])
    applicability = settings.get("anchor_applicability", {})
    issues = []
    for key, sample in ann["samples"].items():
        t = int(key)
        context = cx.at(t)
        applicable = applicability.get(context)
        for f in ANCHOR_FIELDS:
            value = sample.get(f)
            if not value:
                continue
            if applicable is None:
                message = f"{fmt(t)} {f} 请先选择已配置适用关系的空间情境"
            elif f not in applicable and value != "NA":
                message = f"{fmt(t)} {f} 在 {context} 中不适用，必须为 NA"
            elif f in applicable and value not in ("V", "N", "U"):
                message = f"{fmt(t)} {f} 在 {context} 中适用，只能填写 V/N/U"
            else:
                continue
            issues.append(_issue("error", "anchors", message, t))
    return issues


def fill_inapplicable_anchors(ann: dict, settings: dict) -> None:
    """保存时为已有采样点补齐固定 NA；调用前必须先校验，不覆盖非法输入。"""
    cx = Lookup(ann["context"])
    for key, sample in ann["samples"].items():
        applicable = settings.get("anchor_applicability", {}).get(cx.at(int(key)))
        if applicable is not None:
            for f in ANCHOR_FIELDS:
                if f not in applicable:
                    sample[f] = "NA"


def merge_candidates(events: list[dict], recovery_min_stable_ms: int) -> list[tuple[str, str]]:
    """相邻两次定向丢失之间没有出现持续 ≥2 s 的 SCS=2 时应合并。

    判断依据：前一事件的恢复点 + 2 s 晚于后一事件起点（或前一事件没有恢复点且间隔不足 2 s）。
    """
    evs = sorted(events, key=lambda e: e["onset_ms"])
    out = []
    for a, b in zip(evs, evs[1:]):
        rec = a.get("recovery_ms")
        if rec is None:
            if b["onset_ms"] - a["offset_ms"] < recovery_min_stable_ms:
                out.append((a["id"], b["id"]))
        elif b["onset_ms"] < rec + recovery_min_stable_ms:
            out.append((a["id"], b["id"]))
    return out


def event_merge_issues(ann: dict, settings: dict) -> list[dict]:
    """应合并的事件属于阻断错误；草稿允许保留，最终数据不得分开计数。"""
    events = [e for e in ann["events"] if e.get("offset_ms") is not None]
    minimum = settings["recovery_min_stable_ms"]
    by_id = {e["id"]: e for e in events}
    return [
        _issue("error", "events",
               f"{a_id} 与 {b_id} 之间未出现持续 ≥{minimum / 1000:g}s 的 SCS=2，必须合并后才能提交、复核通过或锁定",
               by_id[b_id]["onset_ms"], b_id, fix={"type": "merge_events", "ids": [a_id, b_id]})
        for a_id, b_id in merge_candidates(events, minimum)
    ]


def validate(case: dict, ann: dict, settings: dict, mask_objects: list[dict] | None = None) -> list[dict]:
    dur = duration_ms_of(case)
    issues: list[dict] = anchor_issues(ann, settings)
    gold = bool(case.get("gold_set"))

    # 轨道 0/1：全程覆盖
    for key, track, name in (("evaluable", "evaluable", "可评价状态"), ("context", "context", "空间情境")):
        for iv in intervals(ann[key], dur):
            if iv["label"] is None:
                issues.append(_issue("error", track, f"{name}未标注：{fmt(iv['start_ms'])}–{fmt(iv['end_ms'])}", iv["start_ms"]))

    # 轨道 2/3：采样点
    missing_scs, missing_anchor = [], []
    for r in sample_rows(case, ann, settings):
        if r["evaluable"] != 1:
            continue
        if not r["SCS"]:
            missing_scs.append(r["time_ms"])
        if any(not r[f] for f in ANCHOR_FIELDS):
            missing_anchor.append(r["time_ms"])
    if missing_scs:
        issues.append(_issue("error", "scs", f"{len(missing_scs)} 个可评价采样点缺少 SCS（首个 {fmt(missing_scs[0])}）", missing_scs[0]))
    if missing_anchor:
        issues.append(_issue("error", "anchors", f"{len(missing_anchor)} 个可评价采样点空间锚点未填全（首个 {fmt(missing_anchor[0])}）", missing_anchor[0]))

    # 轨道 4/5：定向丢失与恢复
    ev = Lookup(ann["evaluable"])
    evs = sorted(ann["events"], key=lambda e: e["onset_ms"])
    min_dur = settings["ol_min_duration_ms"]
    for e in evs:
        on, off, rec = e.get("onset_ms"), e.get("offset_ms"), e.get("recovery_ms")
        if on is None or off is None:
            issues.append(_issue("error", "events", f"{e['id']} 缺少开始或结束时间", on, e["id"]))
            continue
        if off <= on:
            issues.append(_issue("error", "events", f"{e['id']} 结束时间早于开始时间", on, e["id"]))
        elif off - on < min_dur:
            issues.append(_issue("error", "events", f"{e['id']} 持续 {(off - on) / 1000:.3f}s，短于预设 {min_dur / 1000:g}s，不能提交或锁定", on, e["id"]))
        if rec is None:
            issues.append(_issue("error", "recovery", f"{e['id']} 缺少空间恢复时间点", off, e["id"]))
        elif rec < off:
            issues.append(_issue("error", "recovery", f"{e['id']} 恢复时间点早于事件结束", rec, e["id"]))
        if ev.at(on) == "E0":
            issues.append(_issue("warning", "events", f"{e['id']} 起点位于不可评价区间（原则上不新判定起点）", on, e["id"]))
        if not e.get("disturbance"):
            issues.append(_issue("warning", "events", f"{e['id']} 未记录主要干扰因素", on, e["id"]))
        # 事件内出现 SCS=2 的采样点 → 前后不一致
        for t_str, s in ann["samples"].items():
            t = int(t_str)
            if on <= t < off and s.get("scs") == "2":
                issues.append(_issue("warning", "scs", f"{fmt(t)} SCS=2 位于定向丢失事件 {e['id']} 内", t, e["id"]))
    for a, b in zip(evs, evs[1:]):
        if a.get("offset_ms") is not None and b["onset_ms"] < a["offset_ms"]:
            issues.append(_issue("error", "events", f"{a['id']} 与 {b['id']} 时间重叠", b["onset_ms"], b["id"]))
    issues.extend(event_merge_issues(ann, settings))
    # SCS=0 采样点不在任何事件内 → 提示可能漏标
    for t_str, s in ann["samples"].items():
        t = int(t_str)
        if s.get("scs") == "0" and ev.at(t) == "E1" and not any(
            e.get("offset_ms") is not None and e["onset_ms"] <= t < e["offset_ms"] for e in evs
        ):
            issues.append(_issue("info", "scs", f"{fmt(t)} SCS=0 但不在任何定向丢失事件内，请确认是否漏标", t))

    # 轨道 6：里程碑
    if gold:
        last_t, last_m = -1, None
        for m in MILESTONES:
            v = ann["milestones"].get(m["code"])
            if v is None or (v.get("assessable", 1) == 1 and v.get("time_ms") is None):
                issues.append(_issue("error", "milestones", f"{m['code']} {m['label']} 未标注（无法判断请标记不可评价）"))
                continue
            if v.get("assessable", 1) == 1:
                t = v["time_ms"]
                if t < last_t:
                    issues.append(_issue("info", "milestones", f"{m['code']} 早于 {last_m}，请确认顺序", t))
                last_t, last_m = t, m["code"]
        # 轨道 7：掩膜
        if mask_objects is not None:
            if not mask_objects:
                issues.append(_issue("error", "masks", "金标准病例尚无关键结构分割（轨道 7）"))
            for o in mask_objects:
                if not o["verified"]:
                    issues.append(_issue("error", "masks", f"{o['object_id']}（{o['class']}）尚未经专家复核确认", o["start_ms"]))
    return issues


# ---------------------------------------------------------------- 指标


def metrics(case: dict, ann: dict, settings: dict, interval_ms: int | None = None) -> dict:
    dur = duration_ms_of(case)
    rows = sample_rows(case, ann, settings, interval_ms)
    ev_rows = [r for r in rows if r["evaluable"] == 1]
    scs_valid = [r for r in ev_rows if r["SCS"] in SCS_VALUES]
    n2 = sum(1 for r in scs_valid if r["SCS"] == "2")
    evaluable_ms = sum(iv["end_ms"] - iv["start_ms"] for iv in intervals(ann["evaluable"], dur) if iv["label"] == "E1")
    events = [e for e in event_rows(ann) if e.get("offset_ms") is not None]
    srts = [e["SRT_ms"] for e in events if e["SRT_ms"] is not None]
    lps = {}
    for f in ANCHOR_FIELDS:
        c = Counter(r[f] for r in ev_rows
                    if f in settings.get("anchor_applicability", {}).get(r["context"], []))
        denom = c["V"] + c["N"]
        lps[f] = c["V"] / denom if denom else None
    return {
        "n_samples": len(rows),
        "n_evaluable_samples": len(ev_rows),
        "n_scs_valid": len(scs_valid),
        "scs_stable_ratio": n2 / len(scs_valid) if scs_valid else None,
        "evaluable_duration_sec": evaluable_ms / 1000,
        "ol_events": len(events),
        "olf_per_10min": len(events) / (evaluable_ms / 600_000) if evaluable_ms else None,
        "srt_mean_ms": sum(srts) / len(srts) if srts else None,
        "srt_median_ms": _median(srts),
        "lps": lps,
    }


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# ---------------------------------------------------------------- 一致性


def cohen_kappa(a: list, b: list, categories: list | None = None, weights: str | None = None) -> float | None:
    """Cohen's κ；weights='linear' / 'quadratic' 时为加权 κ（类别需按顺序给出）。"""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return None
    cats = categories or sorted({x for p in pairs for x in p})
    pairs = [p for p in pairs if p[0] in cats and p[1] in cats]
    if not pairs:
        return None
    k = len(cats)
    idx = {c: i for i, c in enumerate(cats)}
    n = len(pairs)
    obs = [[0.0] * k for _ in range(k)]
    for x, y in pairs:
        obs[idx[x]][idx[y]] += 1 / n
    ra = [sum(obs[i]) for i in range(k)]
    rb = [sum(obs[i][j] for i in range(k)) for j in range(k)]

    def w(i, j):
        if weights == "linear":
            return abs(i - j) / (k - 1) if k > 1 else 0
        if weights == "quadratic":
            return ((i - j) / (k - 1)) ** 2 if k > 1 else 0
        return 0.0 if i == j else 1.0

    po = sum(w(i, j) * obs[i][j] for i in range(k) for j in range(k))
    pe = sum(w(i, j) * ra[i] * rb[j] for i in range(k) for j in range(k))
    if pe == 0:
        return 1.0 if po == 0 else None
    return 1 - po / pe


def _grid(parts, dur, step):
    lk = Lookup(parts)
    return [lk.at(t) for t in range(0, dur, step)]


def _boundaries(parts, dur):
    ivs = [iv for iv in intervals(parts, dur) if iv["label"] is not None]
    return [(b["start_ms"], a["label"], b["label"]) for a, b in zip(ivs, ivs[1:])]


def _event_mask(events, dur, step):
    m = [False] * len(range(0, dur, step))
    for e in events:
        if e.get("offset_ms") is None:
            continue
        for i in range(e["onset_ms"] // step, min(len(m), math.ceil(e["offset_ms"] / step))):
            m[i] = True
    return m


def pair_sequences(case: dict, a: dict, b: dict, settings: dict) -> dict:
    """两位标注者在同一病例上的配对序列，供单病例或跨病例汇总计算一致性。"""
    dur = duration_ms_of(case)
    step = 1000
    ra = {r["time_ms"]: r for r in sample_rows(case, a, settings)}
    rb = {r["time_ms"]: r for r in sample_rows(case, b, settings)}
    common = [t for t in ra if t in rb and ra[t]["evaluable"] == 1 and rb[t]["evaluable"] == 1]
    seq = {
        "context": (_grid(a["context"], dur, step), _grid(b["context"], dur, step)),
        "evaluable": (_grid(a["evaluable"], dur, step), _grid(b["evaluable"], dur, step)),
        "scs": ([ra[t]["SCS"] for t in common], [rb[t]["SCS"] for t in common]),
        "anchors": {f: ([ra[t][f] for t in common], [rb[t][f] for t in common]) for f in ANCHOR_FIELDS},
        "event_mask": (_event_mask(a["events"], dur, 100), _event_mask(b["events"], dur, 100)),
        "boundary_errors": [],
        "boundary_unmatched": 0,
        "event_match": _match_events(a["events"], b["events"]),
        "milestones": {},
    }
    bb = _boundaries(b["context"], dur)
    for t, la, lb in _boundaries(a["context"], dur):
        cands = [abs(t - t2) for t2, x, y in bb if (x, y) == (la, lb)]
        if cands:
            seq["boundary_errors"].append(min(cands))
        else:
            seq["boundary_unmatched"] += 1
    for m in MILESTONES:
        va, vb = a["milestones"].get(m["code"]), b["milestones"].get(m["code"])
        if va and vb:
            seq["milestones"][m["code"]] = {
                "assessable": (va.get("assessable", 1), vb.get("assessable", 1)),
                "diff_ms": abs(va["time_ms"] - vb["time_ms"])
                if va.get("assessable", 1) and vb.get("assessable", 1) and va.get("time_ms") is not None and vb.get("time_ms") is not None
                else None,
            }
    return seq


def _iou(e1, e2):
    inter = max(0, min(e1["offset_ms"], e2["offset_ms"]) - max(e1["onset_ms"], e2["onset_ms"]))
    union = max(e1["offset_ms"], e2["offset_ms"]) - min(e1["onset_ms"], e2["onset_ms"])
    return inter / union if union > 0 else 0.0


def _match_events(ea, eb):
    ea = [e for e in ea if e.get("offset_ms") is not None]
    eb = [e for e in eb if e.get("offset_ms") is not None]
    cands = sorted(((_iou(x, y), i, j) for i, x in enumerate(ea) for j, y in enumerate(eb)), reverse=True)
    used_a, used_b, matches = set(), set(), []
    for iou, i, j in cands:
        if iou <= 0 or i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        x, y = ea[i], eb[j]
        matches.append({
            "a": x["id"], "b": y["id"], "iou": iou,
            "onset_err_ms": abs(x["onset_ms"] - y["onset_ms"]),
            "offset_err_ms": abs(x["offset_ms"] - y["offset_ms"]),
            "recovery_err_ms": abs(x["recovery_ms"] - y["recovery_ms"])
            if x.get("recovery_ms") is not None and y.get("recovery_ms") is not None else None,
        })
    return {"n_a": len(ea), "n_b": len(eb), "matches": matches}


def _concat(seqs, key):
    a, b = [], []
    for s in seqs:
        a += s[key][0]
        b += s[key][1]
    return a, b


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def summarize(seqs: list[dict]) -> dict:
    from .protocol import CONTEXTS

    ctx_codes = [c["code"] for c in CONTEXTS]
    ca, cb = _concat(seqs, "context")
    ea, eb = _concat(seqs, "evaluable")
    sa, sb = _concat(seqs, "scs")
    ma, mb = _concat(seqs, "event_mask")
    tiou = {}
    for c in ctx_codes:
        inter = sum(1 for x, y in zip(ca, cb) if x == c and y == c)
        union = sum(1 for x, y in zip(ca, cb) if x == c or y == c)
        if union:
            tiou[c] = inter / union
    anchors = {}
    for f in ANCHOR_FIELDS:
        xa, xb = [], []
        for s in seqs:
            xa += s["anchors"][f][0]
            xb += s["anchors"][f][1]
        anchors[f] = cohen_kappa(xa, xb, VIS_VALUES)
    matches = [m for s in seqs for m in s["event_match"]["matches"]]
    n_a = sum(s["event_match"]["n_a"] for s in seqs)
    n_b = sum(s["event_match"]["n_b"] for s in seqs)
    inter = sum(1 for x, y in zip(ma, mb) if x and y)
    union = sum(1 for x, y in zip(ma, mb) if x or y)
    ms = {}
    for m in MILESTONES:
        diffs = [s["milestones"][m["code"]]["diff_ms"] for s in seqs if m["code"] in s["milestones"]]
        both = [s["milestones"][m["code"]]["assessable"] for s in seqs if m["code"] in s["milestones"]]
        if both:
            ms[m["code"]] = {
                "n": len(both),
                "assessable_agreement": sum(1 for x, y in both if x == y) / len(both),
                "mean_abs_diff_ms": _mean(diffs),
            }
    bes = [e for s in seqs for e in s["boundary_errors"]]
    return {
        "n_cases": len(seqs),
        "context": {
            "kappa": cohen_kappa(ca, cb, ctx_codes),
            "temporal_iou": tiou,
            "boundary_mean_abs_err_ms": _mean(bes),
            "boundary_unmatched": sum(s["boundary_unmatched"] for s in seqs),
        },
        "evaluable": {"kappa": cohen_kappa(ea, eb, ["E1", "E0"])},
        "scs": {
            "n_pairs": len(sa),
            "weighted_kappa_linear": cohen_kappa(sa, sb, SCS_VALUES, "linear"),
            "weighted_kappa_quadratic": cohen_kappa(sa, sb, SCS_VALUES, "quadratic"),
            "u_rate_a": sa.count("U") / len(sa) if sa else None,
            "u_rate_b": sb.count("U") / len(sb) if sb else None,
        },
        "anchors": anchors,
        "events": {
            "n_a": n_a,
            "n_b": n_b,
            "matched": len(matches),
            "f1": 2 * len(matches) / (n_a + n_b) if n_a + n_b else None,
            "temporal_iou": inter / union if union else None,
            "mean_onset_err_ms": _mean([m["onset_err_ms"] for m in matches]),
            "mean_offset_err_ms": _mean([m["offset_err_ms"] for m in matches]),
            "mean_recovery_err_ms": _mean([m["recovery_err_ms"] for m in matches]),
        },
        "milestones": ms,
    }


def icc_a1(matrix: list[list[float]]) -> float | None:
    """ICC(A,1)：双向随机、绝对一致、单测量。matrix 为 n 个病例 × k 种条件。"""
    rows = [r for r in matrix if all(v is not None for v in r)]
    n = len(rows)
    if n < 2:
        return None
    k = len(rows[0])
    grand = sum(sum(r) for r in rows) / (n * k)
    rm = [sum(r) / k for r in rows]
    cm = [sum(r[j] for r in rows) / n for j in range(k)]
    msr = k * sum((m - grand) ** 2 for m in rm) / (n - 1)
    msc = n * sum((m - grand) ** 2 for m in cm) / (k - 1)
    sse = sum((rows[i][j] - rm[i] - cm[j] + grand) ** 2 for i in range(n) for j in range(k))
    mse = sse / ((n - 1) * (k - 1))
    denom = msr + (k - 1) * mse + k * (msc - mse) / n
    if denom == 0:
        return 1.0
    return (msr - mse) / denom


def fmt(ms: int | None) -> str:
    if ms is None:
        return "-"
    s = ms / 1000
    return f"{int(s // 60):02d}:{s % 60:06.3f}"
