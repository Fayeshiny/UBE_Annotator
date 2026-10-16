"""标注方案 Protocol v1.0 中的编码定义。

所有编码、字段名与《UBE 外科空间智能（SSI）标注执行方案》保持一致；
前端通过 /api/protocol 读取本文件内容，避免两端各写一份。
"""

PROTOCOL_VERSION = "v1.0"

EVALUABLE = [
    {"code": "E1", "label": "可评价", "desc": "画面足以判断当前空间情境、术者行为和主要结构可见性"},
    {"code": "E0", "label": "不可评价", "desc": "持续黑屏、严重失焦、镜头完全被组织/血液覆盖、视频损坏等"},
]

CONTEXTS = [
    {"code": "C1", "label": "骨性空间建立", "desc": "主要依靠椎板、关节突等建立初始空间方向", "milestones": "M1–M3"},
    {"code": "C2", "label": "椎板间窗/黄韧带暴露", "desc": "椎板间窗和黄韧带成为主要视觉与操作界面", "milestones": "M4–M5"},
    {"code": "C3", "label": "椎管进入", "desc": "黄韧带打开并开始进入、确认硬膜外空间", "milestones": "M6–M7"},
    {"code": "C4", "label": "神经结构识别", "desc": "硬膜囊和/或行走神经根得到可靠识别", "milestones": "M8–M9"},
    {"code": "C5", "label": "减压与空间操控", "desc": "神经根松解、髓核处理及减压空间调整成为主要操作", "milestones": "M10–M14"},
    {"code": "C6", "label": "稳定减压与终点确认", "desc": "实质性减压完成，进入稳定减压与最终检查", "milestones": "M15–M18"},
    {"code": "CU", "label": "过渡/不确定", "desc": "阶段转换边界暂不能可靠归入任一主要情境", "milestones": ""},
]

# 程序字段名与 sampled_ssi.csv 列名一致
ANCHORS = [
    {"code": "A1", "field": "lamina", "label": "椎板", "en": "lamina"},
    {"code": "A2", "field": "facet", "label": "关节突内缘", "en": "medial facet border"},
    {"code": "A3", "field": "LF_window", "label": "椎板间窗/黄韧带区域", "en": "interlaminar window / LF"},
    {"code": "A4", "field": "dura", "label": "硬膜囊", "en": "dura"},
    {"code": "A5", "field": "root", "label": "行走神经根", "en": "traversing root"},
]
ANCHOR_FIELDS = [a["field"] for a in ANCHORS]

VISIBILITY = [
    {"code": "V", "label": "清晰可见"},
    {"code": "N", "label": "适用但当前不可见"},
    {"code": "U", "label": "疑似，不能确认"},
    {"code": "NA", "label": "当前情境不适用"},
]

SCS = [
    {"code": "2", "label": "空间稳定"},
    {"code": "1", "label": "空间不稳定"},
    {"code": "0", "label": "空间定向丢失状态"},
    {"code": "U", "label": "无法判断"},
]

DISTURBANCES = [
    {"code": "none", "label": "无明显干扰"},
    {"code": "blood", "label": "血液/液体遮挡"},
    {"code": "defocus", "label": "失焦"},
    {"code": "instrument", "label": "器械遮挡"},
    {"code": "camera_reposition", "label": "镜头大幅重新定位"},
    {"code": "tissue", "label": "组织形变/操作"},
    {"code": "other", "label": "其他"},
]

CONFIDENCE = [
    {"code": "high", "label": "高"},
    {"code": "medium", "label": "中"},
    {"code": "low", "label": "低"},
]

MILESTONES = [
    {"code": "M1", "label": "中线参考建立", "en": "midline establishment", "context": "C1"},
    {"code": "M2", "label": "椎板参考建立", "en": "lamina reference established", "context": "C1"},
    {"code": "M3", "label": "关节突内缘确认", "en": "medial facet identified", "context": "C1"},
    {"code": "M4", "label": "椎板间窗建立", "en": "interlaminar window established", "context": "C2"},
    {"code": "M5", "label": "黄韧带完整暴露", "en": "LF fully exposed", "context": "C2"},
    {"code": "M6", "label": "黄韧带开口", "en": "LF opening", "context": "C3"},
    {"code": "M7", "label": "硬膜外空间建立", "en": "epidural space established", "context": "C3"},
    {"code": "M8", "label": "硬膜囊识别", "en": "dura identified", "context": "C4"},
    {"code": "M9", "label": "行走神经根识别", "en": "traversing root identified", "context": "C4"},
    {"code": "M10", "label": "神经根松解开始", "en": "root mobilization begins", "context": "C5"},
    {"code": "M11", "label": "压迫来源定位", "en": "compression source localized", "context": "C5"},
    {"code": "M12", "label": "隐藏区域探查", "en": "hidden-fragment exploration", "context": "C5"},
    {"code": "M13", "label": "髓核摘除", "en": "fragment extraction", "context": "C5"},
    {"code": "M14", "label": "减压通道扩展", "en": "decompression corridor expanded", "context": "C5"},
    {"code": "M15", "label": "神经根充分游离", "en": "root freely mobilized", "context": "C6"},
    {"code": "M16", "label": "搏动恢复", "en": "pulsation restored", "context": "C6"},
    {"code": "M17", "label": "稳定减压状态建立", "en": "stable decompression state", "context": "C6"},
    {"code": "M18", "label": "终点确认", "en": "endpoint confirmation", "context": "C6"},
]

# 轨道 7 的关键结构类别（与 mask_index.csv 的 class 列一致）
MASK_CLASSES = [
    {"code": "lamina", "label": "椎板", "color": "#f59e0b"},
    {"code": "facet", "label": "关节突内缘", "color": "#10b981"},
    {"code": "LF_window", "label": "椎板间窗/黄韧带", "color": "#3b82f6"},
    {"code": "dura", "label": "硬膜囊", "color": "#ec4899"},
    {"code": "root", "label": "行走神经根", "color": "#a855f7"},
]

ANNOTATION_SOURCES = [
    {"code": "manual", "label": "人工", "gold_eligible": True},
    {"code": "sam_propagated", "label": "模型传播（未复核）", "gold_eligible": False},
    {"code": "sam_corrected", "label": "模型辅助校正", "gold_eligible": True},
]

# 默认项目设置。anchor_applicability 控制不适用锚点的 NA 锁定及 LPS 统计；
# 具体适用关系需在 Pilot 中由专家确认后冻结。
DEFAULT_SETTINGS = {
    "sampling_interval_ms": 5000,
    "sample_review_window_ms": 2500,
    "ol_min_duration_ms": 5000,
    "recovery_min_stable_ms": 2000,
    "rules_frozen": False,
    "anchor_applicability": {
        "C1": ["lamina", "facet", "LF_window"],
        "C2": ["lamina", "facet", "LF_window"],
        "C3": ["lamina", "facet", "LF_window", "dura"],
        "C4": ["lamina", "facet", "LF_window", "dura", "root"],
        "C5": ["lamina", "facet", "LF_window", "dura", "root"],
        "C6": ["lamina", "facet", "LF_window", "dura", "root"],
        "CU": ["lamina", "facet", "LF_window", "dura", "root"],
    },
}


def protocol_dict() -> dict:
    return {
        "version": PROTOCOL_VERSION,
        "evaluable": EVALUABLE,
        "contexts": CONTEXTS,
        "anchors": ANCHORS,
        "visibility": VISIBILITY,
        "scs": SCS,
        "disturbances": DISTURBANCES,
        "confidence": CONFIDENCE,
        "milestones": MILESTONES,
        "mask_classes": MASK_CLASSES,
        "annotation_sources": ANNOTATION_SOURCES,
    }


def empty_annotation() -> dict:
    """单个标注任务（assignment）的标注数据结构。

    evaluable / context 用“分段起点”列表表示时间轴的完整划分：
    每段从 start_ms 开始，到下一段 start_ms（或视频结尾）结束，因此不会出现缝隙或重叠。
    label 为 None 表示尚未标注。
    """
    return {
        "evaluable": [{"start_ms": 0, "label": None}],
        "context": [{"start_ms": 0, "label": None}],
        "samples": {},  # str(time_ms) -> {"scs": "2", "lamina": "V", ..., "note": ""}
        "events": [],  # [{"id","onset_ms","offset_ms","recovery_ms","disturbance","confidence","note"}]
        "milestones": {},  # "M1" -> {"time_ms": int|None, "assessable": 1|0, "confidence": str, "note": str}
    }
