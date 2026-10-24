"""Register one business tool, then use the configured real model."""

import argparse
import json
from pathlib import Path

from miniagent.config import load_agent
from miniagent.tools import Tool, ToolArgs


class RequirementArgs(ToolArgs):
    requirement_id: str


def lookup_requirement(requirement_id: str) -> dict:
    data = json.loads(Path('workspace/requirements.json').read_text(encoding='utf-8'))
    if requirement_id not in data:
        raise ValueError('Requirement not found.')
    return {'requirement_id': requirement_id, 'text': data[requirement_id]}


def main():
    parser = argparse.ArgumentParser(description='真实模型调用自定义需求查询工具')
    parser.add_argument('task')
    args = parser.parse_args()
    agent = load_agent()
    agent.add_tool(Tool('lookup_requirement', 'Look up a requirement by its original ID.',
                        RequirementArgs, lookup_requirement, read_only=True))
    result = agent.run(args.task)
    print(result.answer if result.status == 'completed' else f'{result.status}: {result.error}')
    return 0 if result.status == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
