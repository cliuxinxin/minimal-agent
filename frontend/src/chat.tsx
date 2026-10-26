import { createRoot } from "react-dom/client";
import { useSyncExternalStore } from "react";
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { Thread } from "./components/assistant-ui/elements/thread.aui";
import { convertMessage, type ConversationInput } from "./message-adapter";
export { stopCurrentRun, submitRunInput, directionRequest } from "./stop-run";
export { FRONTEND_VERSION, assessRuntimeCompatibility, createRuntimeDiagnostic } from "./runtime-compatibility";
export { normalizeArtifacts, normalizeSources, normalizeMetrics, formatMetrics, normalizeWorkPlan, normalizeSteering, directionStatus, readablePlanEvidence } from "./workbench-data";
type Message = ConversationInput["messages"][number];

type Snapshot = {
  sessionId: string | null;
  messages: ThreadMessageLike[];
  isRunning: boolean;
  activeRunId?: string;
  busy: boolean;
};
type Actions = { send: (text: string) => Promise<void>; cancel: () => Promise<void> };
const running = (status?: string) => status === "running" || status === "stopping";
const identity = (message: ThreadMessageLike) => message;

// The Python host owns runs, history, and tool execution. This store adapts its
// SSE snapshots to assistant-ui without sending provider requests from React.
export function mountConversation(element: HTMLElement, actions: Actions) {
  let snapshot: Snapshot = { sessionId: null, messages: [], isRunning: false, busy: false };
  let cache = new WeakMap<Message, ThreadMessageLike>();
  const listeners = new Set<() => void>();
  const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
  const getSnapshot = () => snapshot;
  const onNew = async (message: AppendMessage) => {
    const text = message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n");
    if (text) await actions.send(text);
  };
  function RuntimeThread({ value }: { value: Snapshot }) {
    const runtime = useExternalStoreRuntime({
      messages: value.messages,
      convertMessage: identity,
      isRunning: value.isRunning,
      activeRunId: value.activeRunId,
      isDisabled: value.busy,
      onNew,
      onCancel: actions.cancel,
      unstable_enableToolInvocations: false,
    });
    return <AssistantRuntimeProvider runtime={runtime}><Thread /></AssistantRuntimeProvider>;
  }
  function Conversation() {
    const value = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
    return <RuntimeThread key={value.sessionId || "draft"} value={value} />;
  }
  const root = createRoot(element);
  root.render(<Conversation />);
  return {
    update(input: ConversationInput) {
      if (snapshot.sessionId !== input.sessionId) cache = new WeakMap();
      const messages = input.messages.map((message, index) => {
        const previous = cache.get(message);
        if (previous && index < input.messages.length - 1) return previous;
        const converted = convertMessage(message, index, input);
        cache.set(message, converted);
        return converted;
      });
      snapshot = { sessionId: input.sessionId, messages, isRunning: running(input.run?.status),
        activeRunId: running(input.run?.status) ? input.run?.id : undefined, busy: Boolean(input.busy) };
      for (const listener of listeners) listener();
    },
    destroy() { root.unmount(); listeners.clear(); },
  };
}
