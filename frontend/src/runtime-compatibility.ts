// Embedded by build.mjs from package.json; refreshing an old Python process
// can load new assets, so the page must keep its own build identity.
export const FRONTEND_VERSION = __FRONTEND_VERSION__;

type BackendIdentity = {
  app: "minimal-agent-web" | null;
  version: string | null;
  workspace_id: string | null;
  source_revision: string | null;
};

export type RuntimeCompatibility = {
  status: "compatible" | "version_mismatch" | "invalid_identity";
  frontendVersion: string;
  backend: BackendIdentity;
  blocksNewRuns: boolean;
};

function version(value: unknown): string | null {
  return typeof value === "string" && value.length <= 64 &&
    /^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?(?:\+[a-zA-Z0-9.-]+)?$/.test(value) ? value : null;
}

function identity(value: unknown): BackendIdentity {
  const runtime = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
  return {
    app: runtime.app === "minimal-agent-web" ? runtime.app : null,
    version: version(runtime.version),
    workspace_id: typeof runtime.workspace_id === "string" && /^[a-f0-9]{12}$/.test(runtime.workspace_id)
      ? runtime.workspace_id : null,
    source_revision: typeof runtime.source_revision === "string" && /^[a-f0-9]{64}$/.test(runtime.source_revision)
      ? runtime.source_revision : null,
  };
}

// Call only for a received identity. A failed HTTP request is a connection
// problem, not evidence that the service is running a different version.
export function assessRuntimeCompatibility(runtime: unknown, frontendVersion = FRONTEND_VERSION): RuntimeCompatibility {
  const backend = identity(runtime);
  const status = !backend.app || !backend.version || !version(frontendVersion) ? "invalid_identity"
    : backend.version === frontendVersion ? "compatible" : "version_mismatch";
  return { status, frontendVersion, backend, blocksNewRuns: status !== "compatible" };
}

export function createRuntimeDiagnostic(compatibility: RuntimeCompatibility,
  environment: { pageOrigin: string; checkedAt: string | null }) {
  let origin: string | null = null;
  try {
    const page = new URL(environment.pageOrigin);
    if (page.protocol === "http:" || page.protocol === "https:") origin = page.origin;
  } catch { /* Invalid or non-web locations are not useful diagnostics. */ }
  const checked = environment.checkedAt;
  const checkedAt = typeof checked === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(checked)
    && Number.isFinite(Date.parse(checked)) ? checked : null;
  // Explicit allowlist: never copy the bootstrap object, configuration, chat,
  // filesystem paths, URL credentials, or query parameters into this report.
  const safe = assessRuntimeCompatibility(compatibility.backend, compatibility.frontendVersion);
  return { schema_version: 1, kind: "minimal-agent-runtime",
    frontend_version: version(safe.frontendVersion), compatibility: safe.status,
    backend: safe.backend, page_origin: origin, checked_at: checkedAt };
}
