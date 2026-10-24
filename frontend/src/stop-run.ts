export type RunContext = {
  sessionId: string | null;
  runId: string | null;
  revision: number;
  status?: string;
};
type StopControls<T> = {
  read: () => RunContext;
  request: (context: RunContext) => Promise<T>;
  apply: (result: T, context: RunContext) => void;
  recover: (error: unknown, context: RunContext) => Promise<void>;
};

// A stop response can arrive after its task has already ended and a new one has
// started in the same session. Never let that response mutate the newer run.
export async function stopCurrentRun<T>(controls: StopControls<T>): Promise<void> {
  const requested = controls.read();
  if (!requested.sessionId || !requested.runId || requested.status !== "running") return;
  const current = () => {
    const value = controls.read();
    return value.sessionId === requested.sessionId && value.runId === requested.runId &&
      value.revision === requested.revision;
  };
  try {
    const result = await controls.request(requested);
    if (current()) controls.apply(result, requested);
  } catch (error) {
    if (current()) await controls.recover(error, requested);
  }
}
