import { useAuiState } from "@assistant-ui/react";
import { TargetIcon, CheckIcon, CircleIcon, LoaderCircleIcon } from "lucide-react";
import type { WorkPlan, DirectionInput, Source } from "@/workbench-data";
import { directionStatus, readablePlanEvidence } from "@/workbench-data";

export function WorkPlanSnapshot() {
  const plan = useAuiState((s) => s.message.metadata.custom.workPlan) as WorkPlan | null;
  const inputs = useAuiState((s) => s.message.metadata.custom.steering) as DirectionInput[] | undefined;
  const runId = useAuiState((s) => s.message.id);
  const status = useAuiState((s) => s.message.metadata.custom.uiStatus);
  const sources = useAuiState((s) => s.message.metadata.custom.sources) as Source[] | undefined;
  if (!plan && !inputs?.length) return null;
  const verification = plan?.verification;
  return <details className="message-work-plan" data-testid="plan-snapshot">
    <summary><TargetIcon aria-hidden /><span>{plan?.goal || "本轮方向调整"}</span>
      <small>{plan ? `目标与检查 · 第 ${plan.revision} 版` : "查看记录"}</small></summary>
    <div className="message-work-plan-body">
      {plan ? <>
        <h4>完成标准</h4><ul>{plan.success_criteria.map((item, i) => <li key={i}>{item}</li>)}</ul>
        <h4>计划与进度</h4><ol className="plan-steps">{plan.steps.map((step, i) => <li key={i} data-step-status={step.status}>
          {step.status === "completed" ? <CheckIcon aria-hidden /> : step.status === "in_progress" ? <LoaderCircleIcon aria-hidden /> : <CircleIcon aria-hidden />}
          <span>{step.title}</span><small>{step.status === "completed" ? "已完成" : step.status === "in_progress" ? "进行中" : "待处理"}</small>
        </li>)}</ol>
        {plan.summary ? <><h4>当前判断</h4><p>{readablePlanEvidence(plan.summary, sources || [])}</p></> : null}
        {plan.evidence.length ? <><h4>依据与观察</h4><ul>{plan.evidence.map((item, i) => <li key={i}>{readablePlanEvidence(item, sources || [])}</li>)}</ul></> : null}
        {plan.uncertainties.length ? <><h4>仍需确认</h4><ul>{plan.uncertainties.map((item, i) => <li key={i}>{readablePlanEvidence(item, sources || [])}</li>)}</ul></> : null}
        <div className="plan-verification" data-verification={verification?.status}>
          <strong>模型自检：{verification?.status === "passed" ? "认为已满足标准" : verification?.status === "needs_work" ? "仍需完善" : "尚未检查"}</strong>
          {verification?.summary ? <p>{readablePlanEvidence(verification.summary, sources || [])}</p> : null}
          <small>这是模型公开的判断摘要，可结合工具结果和来源核对。</small>
        </div>
      </> : null}
      {inputs?.length ? <div className="plan-directions"><h4>你的方向调整</h4>{inputs.map((input) => <div key={input.id} data-input-id={input.id} data-status={input.status}>
        <span>{directionStatus(input, { id: runId, status: String(status) })}</span><p>{input.message}</p>
      </div>)}</div> : null}
    </div>
  </details>;
}
