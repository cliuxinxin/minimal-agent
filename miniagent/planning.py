"""Bounded, public work-plan updates through the ordinary tool protocol."""
from typing import Annotated, Callable, Literal

from pydantic import Field, StringConstraints, model_validator

from .tools import Tool, ToolArgs


PLANNING_PROMPT = '''For a task involving tools, research, coding or several steps, publish a concise
work plan with update_work_plan before taking substantive action. State the user's goal, observable
completion criteria and a short plan. Update it when important evidence changes the next action,
when a step finishes, or when the user corrects the direction. Ground evidence in actual source
IDs/URLs, inspected files or observed tool/test results. Keep uncertainties explicit.
The summary is a brief public explanation of progress and decisions, not private chain-of-thought.
Before delivering a multi-step task, compare the actual result with the completion criteria and
update verification. This is your own reported check, not a guarantee from the host. Do not mark
success merely because you wrote a plan or called a tool. A plan from an earlier task may be stale;
the latest user instructions take precedence, and a new task may replace the plan. Simple direct
answers do not need a planning call. Keep working after a plan update unless the user asked to pause.
'''


Goal = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Criterion = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
StepTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Evidence = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1200)]


class PlanStep(ToolArgs):
    title: StepTitle
    status: Literal['pending', 'in_progress', 'completed']


class PlanVerification(ToolArgs):
    status: Literal['not_checked', 'needs_work', 'passed']
    summary: Summary


class WorkPlanArgs(ToolArgs):
    goal: Goal
    success_criteria: list[Criterion] = Field(min_length=1, max_length=6)
    steps: list[PlanStep] = Field(min_length=1, max_length=8)
    summary: Summary
    evidence: list[Evidence] = Field(max_length=8)
    uncertainties: list[Criterion] = Field(max_length=6)
    verification: PlanVerification

    @model_validator(mode='after')
    def one_current_step(self):
        if sum(step.status == 'in_progress' for step in self.steps) > 1:
            raise ValueError('At most one plan step can be in progress.')
        return self


def build_plan_tool(handler: Callable) -> Tool:
    return Tool('update_work_plan',
        'Publish or revise the current goal, completion criteria, short plan, public progress summary, '
        'observed evidence, uncertainties and your own completion check. This changes only public '
        'working state; it does not execute the planned work or certify its correctness. '
        'Use concise summaries, not hidden chain-of-thought. Continue executing after the update.',
        WorkPlanArgs, handler, read_only=True)
