export type SessionStatus = 'ACTIVE' | 'COMPLETED';
export type RunStatus = 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED';
export type QualityStatus = 'PASS' | 'WARN' | 'FAIL' | 'UNKNOWN';
export type PreviewStatus = 'PENDING' | 'READY' | 'UNAVAILABLE' | 'FAILED';
export type PostprocessStatus = 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED';

export interface SampleEligibility {
  eligible: boolean;
  reasons: string[];
}

export interface SessionSummary {
  id: string;
  title: string;
  expert_signature?: string | null;
  source_filename: string;
  status: SessionStatus;
  satisfied_run_id?: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface RunSummary {
  id: string;
  session_id: string;
  parent_run_id?: string | null;
  retry_of_run_id?: string | null;
  sequence: number;
  label?: string | null;
  status: RunStatus;
  quality_status: QualityStatus;
  preview_status?: PreviewStatus;
  postprocess_status?: PostprocessStatus;
  postprocess_started_at?: string | null;
  postprocess_finished_at?: string | null;
  postprocess_error?: string | null;
  progress?: number | null;
  created_at: string;
  finished_at?: string | null;
}

export interface SessionDetail extends SessionSummary {
  runs: RunSummary[];
  geometry_summary?: Record<string, unknown> | null;
}

export interface Artifact {
  id: string;
  type: string;
  display_name: string;
  block_id?: string | null;
  size: number;
  mime_type?: string | null;
  sha256?: string | null;
  created_at?: string;
}

export interface RunEvent {
  sequence: number;
  stage: string;
  level: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | string;
  progress?: number | null;
  message: string;
  created_at: string;
  data?: Record<string, unknown> | null;
}

export interface QualityMetric {
  key: string;
  label?: string;
  value?: number | string | null;
  unit?: string | null;
  status?: QualityStatus;
  limit?: number | string | null;
  min?: number | null;
  max?: number | null;
  message?: string | null;
}

export interface QualityReport {
  schema_version?: number;
  status?: QualityStatus;
  metrics?: QualityMetric[] | Record<string, unknown>;
  result?: {
    status?: QualityStatus;
    accepted?: boolean;
    reasons?: string[];
  };
  summary?: string | null;
  [key: string]: unknown;
}

export interface RunDetail extends RunSummary {
  controls?: Record<string, unknown> | null;
  control_changes?: ControlChange[];
  quality?: QualityReport | null;
  sample_eligibility?: SampleEligibility | null;
  experience_note?: string | null;
  note_version?: number;
  events?: RunEvent[];
  artifacts?: Artifact[];
  error_code?: string | null;
  error_message?: string | null;
}

export type ControlAvailability = 'EDITABLE' | 'LOCKED' | 'NOT_APPLICABLE';
export type ControlPriority = 'P0' | 'P1' | 'P2';

export interface ControlOption {
  value: string | number | boolean;
  label: string;
}

export interface ControlItem {
  key: string;
  label: string;
  description?: string | null;
  priority: ControlPriority;
  entity?: string | null;
  selector: string;
  stage?: string | null;
  topology?: string | null;
  availability: ControlAvailability;
  reason?: string | null;
  value_type?: 'boolean' | 'integer' | 'number' | 'string' | 'enum';
  value?: unknown;
  inherited_value?: unknown;
  explicit?: boolean;
  minimum?: number | null;
  maximum?: number | null;
  step?: number | null;
  options?: ControlOption[];
}

export interface ControlState {
  parent_run_id: string;
  controls: ControlItem[];
  entities?: string[];
  stages?: string[];
  topologies?: string[];
}

export type ControlChange =
  | { key: string; selector: string; op: 'set'; value: unknown }
  | { key: string; selector: string; op: 'clear' };

export interface PreviewIssue {
  code?: string;
  key?: string;
  selector?: string;
  message: string;
}

export interface EffectiveAvailabilityEntry {
  key: string;
  selector: string;
  availability: ControlAvailability;
  reason?: string | null;
}

export interface ControlPreview {
  valid: boolean;
  normalized_changes?: ControlChange[];
  expanded_changes?: ControlChange[];
  required_clears?: ControlChange[];
  warnings?: PreviewIssue[];
  errors?: PreviewIssue[];
  effective_availability?: EffectiveAvailabilityEntry[];
}

export interface MeshBlock {
  id: string;
  index?: number;
  base?: string;
  name: string;
  size?: [number, number, number];
  dimensions: [number, number, number] | Record<'I' | 'J' | 'K', number>;
  index_ranges?: Record<'I' | 'J' | 'K', [number, number]>;
  bounds?: [number, number, number, number, number, number] | Record<'x' | 'y' | 'z', [number, number]>;
  surface?: boolean;
  wireframe?: boolean;
  slices?: Partial<Record<'I' | 'J' | 'K', { minimum: number; maximum: number }>>;
  modes?: Array<'surface' | 'wireframe' | string>;
  assets?: Partial<Record<'surface' | 'wireframe', string>>;
}

export interface MeshManifest {
  status: PreviewStatus;
  available?: boolean;
  format?: string | null;
  reason_code?: string | null;
  reason?: string | null;
  source?: Record<string, unknown> | null;
  blocks: MeshBlock[];
  capabilities?: {
    surface?: boolean;
    wireframe?: boolean;
    slice?: boolean;
    slice_index_base?: number;
  };
}

export interface SessionListResponse {
  items: SessionSummary[];
  next_cursor?: string | null;
}

export interface EventsResponse {
  items: RunEvent[];
  next_after?: number | null;
}

export interface HealthSnapshot {
  status: 'ok' | 'degraded';
  database: { status: string; version: number };
  worker: {
    online: boolean;
    id: string | null;
    last_heartbeat: string | null;
    running_count: number;
    max_concurrency: number;
  };
  igg: {
    configured: boolean;
    available: boolean;
    path: string | null;
  };
  queue: {
    queued: number;
    running: number;
    succeeded: number;
    failed: number;
  };
  resource_gate: Record<string, unknown>;
}

export interface AuthSession {
  enabled: boolean;
  authenticated: boolean;
  username: string | null;
}
