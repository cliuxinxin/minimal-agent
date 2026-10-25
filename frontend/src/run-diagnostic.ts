// The local Python host creates and redacts this report. The browser exports
// only this diagnostic object, never conversation bodies or provider payloads.
export type RunDiagnostic = {
  schema_version: number;
  app_version?: string;
  run_id: string;
  session_id?: string;
  started_at?: string;
  finished_at?: string;
  status: string;
  step: number;
  connection?: { provider?: string; model?: string; endpoint?: string; stream?: boolean };
  error?: {
    type?: string;
    message?: string;
    code?: string;
    phase?: string;
    field?: string;
    expected?: string;
    actual_type?: string;
    chunk_index?: number;
    event_type?: string;
    finish_reason?: string;
    http_status?: number;
    request_id?: string;
    elapsed_ms?: number;
    chunk_shape?: unknown;
  };
  tools?: { name: string; call_id?: string; status: string; step?: number; arguments_chars?: number; has_result?: boolean }[];
  events?: { event: string; step?: number; time?: string }[];
  omitted_events?: number;
  log_file?: string | null;
  diagnostic_file?: string | null;
  log_error?: string;
  storage_error?: string;
};
