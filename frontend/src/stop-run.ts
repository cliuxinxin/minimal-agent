export type RunContext = {
  sessionId: string | null;
  runId: string | null;
  revision: number;
  status?: string;
  acceptingInput?: boolean;
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

type DirectionRequest = { sessionId: string | null; runId: string | null; message: string; inputId: string };

export function directionRequest(previous: DirectionRequest | null, context: RunContext,
  message: string, createId: () => string): DirectionRequest {
  return previous && previous.sessionId === context.sessionId && previous.runId === context.runId && previous.message === message
    ? previous : { sessionId: context.sessionId, runId: context.runId, message, inputId: createId() };
}

export async function submitRunInput<T>(controls: Omit<StopControls<T>, "request"> & { inputId: string; message: string;
  request: (context: RunContext & { inputId: string; message: string }) => Promise<T> }): Promise<"accepted" | "stale" | "rejected" | "unavailable"> {
  const requested = { ...controls.read(), inputId: controls.inputId, message: controls.message.trim() };
  if (!requested.sessionId || !requested.runId || requested.status !== "running" || requested.acceptingInput === false) return "unavailable";
  const current = () => {
    const value = controls.read();
    return value.sessionId === requested.sessionId && value.runId === requested.runId && value.revision === requested.revision;
  };
  try {
    if (!requested.message || requested.message.length > 8000) throw new Error("补充或纠正内容需要在 1–8000 个字符之间。");
    const result = await controls.request(requested);
    if (!current()) return "stale";
    controls.apply(result, requested);
    return "accepted";
  } catch (error) {
    if (!current()) return "stale";
    await controls.recover(error, requested);
    return "rejected";
  }
}
