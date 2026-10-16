import pytest
import numpy as np

from ssi_annotator import analysis, rle
from ssi_annotator.protocol import DEFAULT_SETTINGS, empty_annotation

CASE = {"case_id": "SSI_T", "video_duration_sec": 60.0, "fps": 30, "gold_set": 0}


def ann_fixture():
    a = empty_annotation()
    a["evaluable"] = [{"start_ms": 0, "label": "E1"}, {"start_ms": 50000, "label": "E0"}]
    a["context"] = [{"start_ms": 0, "label": "C1"}, {"start_ms": 20000, "label": "C2"}, {"start_ms": 40000, "label": "C2"}]
    for t in range(0, 50000, 5000):
        a["samples"][str(t)] = {"scs": "2" if t < 25000 else "1", "lamina": "V", "facet": "N", "LF_window": "V",
                                "dura": "NA", "root": "NA"}
    a["events"] = [
        {"id": "OL001", "onset_ms": 26000, "offset_ms": 33000, "recovery_ms": 36000, "disturbance": "blood", "confidence": "high"},
        {"id": "OL002", "onset_ms": 37000, "offset_ms": 43000, "recovery_ms": 45000, "disturbance": "none", "confidence": "high"},
    ]
    return a


def test_intervals_merge_and_cover():
    ivs = analysis.intervals(ann_fixture()["context"], 60000)
    assert ivs == [
        {"label": "C1", "start_ms": 0, "end_ms": 20000},
        {"label": "C2", "start_ms": 20000, "end_ms": 60000},
    ]


def test_sample_rows_respect_evaluable():
    rows = analysis.sample_rows(CASE, ann_fixture(), DEFAULT_SETTINGS)
    assert len(rows) == 12
    assert rows[-1]["time_ms"] == 55000 and rows[-1]["evaluable"] == 0 and rows[-1]["SCS"] is None
    assert rows[0]["context"] == "C1" and rows[5]["context"] == "C2"


def test_metrics():
    m = analysis.metrics(CASE, ann_fixture(), DEFAULT_SETTINGS)
    assert m["n_evaluable_samples"] == 10
    assert m["scs_stable_ratio"] == 0.5
    assert m["evaluable_duration_sec"] == 50
    assert abs(m["olf_per_10min"] - 2 / (50 / 600)) < 1e-9
    assert m["srt_mean_ms"] == (3000 + 2000) / 2
    assert m["lps"]["lamina"] == 1.0 and m["lps"]["facet"] == 0.0 and m["lps"]["dura"] is None


def test_merge_rule():
    # OL001 恢复于 36 s，OL002 起于 37 s：稳定期不足 2 s → 应合并
    assert analysis.merge_candidates(ann_fixture()["events"], 2000) == [("OL001", "OL002")]
    ev = ann_fixture()["events"]
    ev[1]["onset_ms"] = 38000
    assert analysis.merge_candidates(ev, 2000) == []


def test_validate_flags():
    a = ann_fixture()
    issues = analysis.validate(CASE, a, DEFAULT_SETTINGS)
    msgs = " ".join(i["message"] for i in issues)
    assert any(i.get("fix", {}).get("type") == "merge_events" for i in issues)
    assert "未标注" not in msgs
    a["samples"].pop("10000")
    issues = analysis.validate(CASE, a, DEFAULT_SETTINGS)
    assert any(i["level"] == "error" and i["track"] == "scs" for i in issues)


def test_validate_gold_milestones():
    case = dict(CASE, gold_set=1)
    a = ann_fixture()
    errs = [i for i in analysis.validate(case, a, DEFAULT_SETTINGS, []) if i["track"] == "milestones"]
    assert len(errs) == 18
    a["milestones"] = {f"M{i}": {"time_ms": i * 1000, "assessable": 1, "confidence": "high"} for i in range(1, 19)}
    a["milestones"]["M16"] = {"time_ms": None, "assessable": 0}
    assert not [i for i in analysis.validate(case, a, DEFAULT_SETTINGS, []) if i["track"] == "milestones"]


def test_kappa():
    a = ["0", "1", "2", "2", "1", "0"]
    assert analysis.cohen_kappa(a, a, ["0", "1", "2"]) == 1.0
    assert analysis.cohen_kappa(a, a, ["0", "1", "2"], "linear") == 1.0
    b = ["0", "1", "2", "1", "1", "0"]
    k = analysis.cohen_kappa(a, b, ["0", "1", "2"])
    kw = analysis.cohen_kappa(a, b, ["0", "1", "2"], "linear")
    assert 0 < k < 1 and kw > k


def test_agreement_identical():
    a = ann_fixture()
    s = analysis.summarize([analysis.pair_sequences(CASE, a, a, DEFAULT_SETTINGS)])
    assert s["context"]["kappa"] == 1.0
    assert s["events"]["f1"] == 1.0 and s["events"]["temporal_iou"] == 1.0
    assert s["context"]["boundary_mean_abs_err_ms"] == 0


def test_icc():
    m = [[0.1, 0.12], [0.5, 0.48], [0.9, 0.91], [0.3, 0.3]]
    assert analysis.icc_a1(m) > 0.98


def test_rle_roundtrip():
    rng = np.random.default_rng(0)
    for _ in range(5):
        m = rng.random((37, 53)) > 0.6
        r = rle.encode(m)
        assert (rle.decode(r) == m).all()
        assert rle.area(r) == m.sum()
    full = np.ones((4, 5), bool)
    assert (rle.decode(rle.encode(full)) == full).all()
    assert rle.encode(full)["counts"][0] == 0


def test_lps_filters_applicability_evaluable_and_uncertain():
    from copy import deepcopy

    case = dict(CASE, video_duration_sec=40)
    a = empty_annotation()
    a["context"] = [{"start_ms": 0, "label": "C1"}, {"start_ms": 10000, "label": "C4"}]
    a["evaluable"] = [{"start_ms": 0, "label": "E1"}, {"start_ms": 30000, "label": "E0"}]
    # 故意加入不合规值验证统计过滤，不经保存接口。
    values = ["V", "V", "V", "N", "U", "NA", "V", "N"]
    a["samples"] = {str(t): {"scs": "2", "dura": value} for t, value in zip(range(0, 40000, 5000), values)}
    settings = deepcopy(DEFAULT_SETTINGS)
    assert analysis.metrics(case, a, settings)["lps"]["dura"] == 0.5
    assert analysis.metrics(case, a, settings, interval_ms=10000)["lps"]["dura"] == 1.0
    # 使用配置，不硬编码情境；修改适用关系后分母随之改变。
    settings["anchor_applicability"]["C1"].append("dura")
    assert analysis.metrics(case, a, settings)["lps"]["dura"] == 0.75


def test_lps_no_valid_denominator_is_none():
    a = ann_fixture()
    for sample in a["samples"].values():
        sample["dura"] = "V"  # 全部位于 dura 不适用的 C1/C2
        sample["lamina"] = "U"
    result = analysis.metrics(CASE, a, DEFAULT_SETTINGS)["lps"]
    assert result["dura"] is None and result["lamina"] is None


def test_anchor_rules_validate_and_fill_fixed_na():
    a = ann_fixture()
    a["samples"]["0"]["dura"] = "V"
    a["samples"]["5000"]["lamina"] = "NA"
    issues = analysis.anchor_issues(a, DEFAULT_SETTINGS)
    assert len(issues) == 2
    assert all(i["level"] == "error" for i in issues)
    assert "必须为 NA" in issues[0]["message"]
    assert "只能填写 V/N/U" in issues[1]["message"]
    assert any(i["track"] == "anchors" and i["level"] == "error" for i in analysis.validate(CASE, a, DEFAULT_SETTINGS))
    a["samples"]["0"].pop("dura")
    a["samples"]["5000"].pop("lamina")
    assert not analysis.anchor_issues(a, DEFAULT_SETTINGS)  # 草稿可留空
    rows = analysis.sample_rows(CASE, a, DEFAULT_SETTINGS)
    assert rows[0]["dura"] == "NA"
    analysis.fill_inapplicable_anchors(a, DEFAULT_SETTINGS)
    assert a["samples"]["0"]["dura"] == "NA"
    assert "lamina" not in a["samples"]["5000"]


@pytest.mark.parametrize("duration_ms,blocked", [(1000, True), (4999, True), (5000, False), (5001, False)])
def test_short_orientation_loss_is_blocking_error(duration_ms, blocked):
    a = ann_fixture()
    a["events"] = [{"id": "OL001", "onset_ms": 1000, "offset_ms": 1000 + duration_ms,
                    "recovery_ms": 10000, "disturbance": "none", "confidence": "high"}]
    issues = analysis.validate(CASE, a, DEFAULT_SETTINGS)
    short = [i for i in issues if i["track"] == "events" and "短于预设" in i["message"]]
    assert bool(short) == blocked
    if blocked:
        assert short[0]["level"] == "error" and short[0]["ref"] == "OL001"
        assert short[0]["time_ms"] == 1000 and "不能提交或锁定" in short[0]["message"]
    if duration_ms == 4999:
        assert "4.999s" in short[0]["message"]


def test_orientation_loss_min_duration_uses_project_setting():
    a = ann_fixture()
    a["events"] = [{"id": "OL001", "onset_ms": 1000, "offset_ms": 6000,
                    "recovery_ms": 10000, "disturbance": "none", "confidence": "high"}]
    settings = {**DEFAULT_SETTINGS, "ol_min_duration_ms": 6000}
    issues = analysis.validate(CASE, a, settings)
    assert any(i["level"] == "error" and "短于预设 6s" in i["message"] for i in issues)
