export type Role = "admin" | "reviewer" | "annotator";

export interface User {
  id: number;
  username: string;
  display_name: string;
  role: Role;
  active?: number;
}

export interface Code {
  code: string;
  label: string;
  desc?: string;
  [k: string]: unknown;
}

export interface Anchor {
  code: string;
  field: AnchorField;
  label: string;
  en: string;
}

export type AnchorField = "lamina" | "facet" | "LF_window" | "dura" | "root";

export interface Settings {
  sampling_interval_ms: number;
  sample_review_window_ms: number;
  ol_min_duration_ms: number;
  recovery_min_stable_ms: number;
  rules_frozen: boolean;
  anchor_applicability: Record<string, AnchorField[]>;
}

export interface Protocol {
  version: string;
  evaluable: Code[];
  contexts: Code[];
  anchors: Anchor[];
  visibility: Code[];
  scs: Code[];
  disturbances: Code[];
  confidence: Code[];
  milestones: (Code & { en: string; context: string })[];
  mask_classes: (Code & { color: string })[];
  annotation_sources: Code[];
  settings: Settings;
  sam_backend: string;
}

export interface Part {
  start_ms: number;
  label: string | null;
}

export type Sample = Partial<Record<AnchorField | "scs" | "note", string>>;

export interface OLEvent {
  id: string;
  onset_ms: number;
  offset_ms: number | null;
  recovery_ms: number | null;
  disturbance: string | null;
  confidence: string | null;
  note?: string;
}

export interface Milestone {
  time_ms: number | null;
  assessable: number;
  confidence: string | null;
  note?: string;
}

export interface Annotation {
  evaluable: Part[];
  context: Part[];
  samples: Record<string, Sample>;
  events: OLEvent[];
  milestones: Record<string, Milestone>;
}

export interface AssignmentSummary {
  id: number;
  user_id: number;
  role: string;
  status: string;
  version: number;
  updated_at: number;
  consensus?: ConsensusSummary | null;
  username: string;
  display_name: string;
}

export interface Case {
  case_id: string;
  surgeon_id: string | null;
  center_id: string | null;
  operative_level: string | null;
  cumulative_UBE_cases: number | null;
  gold_set: number;
  video_file: string | null;
  video_duration_sec: number | null;
  fps: number | null;
  frame_count: number | null;
  width: number;
  height: number;
  media_status: string;
  media_error: string | null;
  status: "open" | "locked";
  final_assignment_id: number | null;
  locked_rule_snapshot_id: number | null;
  notes: string | null;
  assignments?: AssignmentSummary[];
}

export interface Issue {
  level: "error" | "warning" | "info";
  track: string;
  message: string;
  time_ms?: number;
  ref?: string;
  fix?: { type: string; ids: string[] };
}

export interface Metrics {
  n_samples: number;
  n_evaluable_samples: number;
  n_scs_valid: number;
  scs_stable_ratio: number | null;
  evaluable_duration_sec: number;
  ol_events: number;
  olf_per_10min: number | null;
  srt_mean_ms: number | null;
  srt_median_ms: number | null;
  lps: Record<AnchorField, number | null>;
}

export interface ConsensusReview {
  id: number;
  reviewer_id: number;
  username: string;
  display_name: string;
  reviewer_role: string;
  decision: "approve" | "reject" | "withdraw";
  comment: string;
  created_at: number;
}

export interface ConsensusSummary {
  required: number;
  creator_excluded: boolean;
  round: number;
  revision: number;
  submitted_at: number | null;
  current: boolean;
  approved_count: number;
  complete: boolean;
  reviews: ConsensusReview[];
}

export interface ConsensusHistory {
  assignment_id: number;
  round: number;
  revision: number;
  reviewer_id: string;
  decision: "approve" | "reject" | "withdraw";
  comment: string;
  created_at: number;
}

export interface AssignmentPayload {
  assignment: {
    id: number;
    case_id: string;
    user_id: number;
    role: string;
    status: string;
    version: number;
    owner: { username: string; display_name: string };
  };
  case: Case;
  data: Annotation;
  editable: boolean;
  issues: Issue[];
  metrics: Metrics;
  settings: Settings;
  applied_rules: {
    source: "locked_snapshot" | "current_project_settings";
    id?: number;
    protocol_version: string;
    settings: Settings;
    settings_sha256: string;
  };
  rules_warning: string | null;
  consensus: ConsensusSummary | null;
}

export interface RLE {
  size: [number, number];
  counts: number[];
}

export interface MaskObject {
  id: number;
  assignment_id: number;
  object_id: string;
  class: string;
  start_ms: number;
  end_ms: number;
  source_frame: number | null;
  annotation_source: string;
  verified: number;
  frame_stride: number;
  n_frames: number;
  note: string | null;
  frames?: { frame_idx: number; source: string }[];
}

export interface FrameMask {
  object_pk: number;
  object_id: string;
  class: string;
  frame_idx: number;
  exact: boolean;
  source: string;
  rle: RLE;
}

export interface SamSession {
  id: string;
  assignment_id: number;
  start_ms: number;
  end_ms: number;
  stride: number;
  status: "loading" | "ready" | "propagating" | "error" | "closed";
  error: string | null;
  progress: number;
  frames: number[];
  objects: number[];
  backend: string | null;
}
