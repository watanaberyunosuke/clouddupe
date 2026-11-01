export type Confidence = 'confirmed' | 'probable' | 'possible';

export interface RemoteInfo {
  name: string;
  type: string;
  description: string;
}

export interface ProviderInfo {
  name: string;
  description: string;
}

export interface Choice {
  value: string;
  help: string;
}

export interface Question {
  name: string;
  help: string;
  default: string;
  examples: Choice[];
  required: boolean;
  isPassword: boolean;
  kind: string;
  exclusive: boolean;
}

export interface ConfigStep {
  done: boolean;
  state: string;
  question: Question | null;
  error: string;
  oauth: boolean;
}

export interface FileEntry {
  idx: number;
  remote: string;
  path: string;
  size: number;
  mod_time: string;
  hashes: Record<string, string>;
}

export interface Group {
  id: string;
  confidence: Confidence;
  evidence: string[];
  members: FileEntry[];
  warnings: string[];
}

export interface RemoteStats {
  files: number;
  bytes: number;
  hash_coverage: number;
  hash_types: string[];
}

export interface Summary {
  generated_at: string;
  tool_version: string;
  total_files: number;
  remotes: Record<string, RemoteStats>;
  comparability: { a: string; b: string; common_hash_types: string[] }[];
  by_confidence: Partial<Record<Confidence, { groups: number; files: number; wasted_bytes: number }>>;
  errors: string[];
}

export interface ScanRequest {
  targets: string[];
  minSize: number;
  sizeOnly: boolean;
  sizeOnlyMin: number;
}

export interface ScanProgress {
  target: string;
  files: number;
  done: boolean;
  error: string | null;
}

export interface ScanResult {
  summary: Summary;
  groups: Group[];
}

export type ExportFormat = 'html' | 'csv' | 'json';
