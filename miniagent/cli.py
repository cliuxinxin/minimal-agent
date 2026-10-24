"""Thin command-line entry point for real API use."""

import argparse
import json
import sys

from .config import load_agent
from .tracing import JsonlTrace


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='最小 Agent：OpenAI / Anthropic 原生工具调用')
    parser.add_argument('task', nargs='?', help='一次性任务；不提供则进入聊天')
    parser.add_argument('--chat', action='store_true', help='持续对话')
    parser.add_argument('--config', default='config.json', help='配置文件路径')
    args = parser.parse_args(argv)
    trace = JsonlTrace()

    def event_sink(event):
        trace(event)
        if event['event'] == 'model_start':
            print(f"[{event['step']}] 调用模型…", file=sys.stderr)
        elif event['event'] == 'tool_call':
            print(f"  工具：{event['data']['name']}", file=sys.stderr)
        elif event['event'] == 'tool_result' and not event['data']['ok']:
            print(f"  工具反馈：{event['data']['error']['code']}", file=sys.stderr)

    def approve(tool, arguments):
        print(f'准备执行写操作 {tool.name}：{json.dumps(arguments, ensure_ascii=False)}')
        return input('允许执行？[y/N] ').strip().lower() == 'y'

    try:
        agent = load_agent(args.config, on_event=event_sink, approve=approve)

        def run(task):
            result = agent.run(task)
            if result.status == 'completed':
                print(result.answer)
            else:
                print(f'运行停止 [{result.status}]：{result.error}', file=sys.stderr)
            if trace.last_path:
                print(f'日志：{trace.last_path}', file=sys.stderr)
            return 0 if result.status == 'completed' else 1

        if args.task:
            status = run(args.task)
            if not args.chat:
                return status
        print('开始对话。/reset 清空历史，/exit 退出。')
        while True:
            try:
                task = input('\n你：').strip()
            except EOFError:
                return 0
            if task == '/exit':
                return 0
            if task == '/reset':
                agent.reset()
                print('对话历史已清空。')
            elif task:
                run(task)
    except KeyboardInterrupt:
        print('\n已停止。', file=sys.stderr)
        return 130
    except (ValueError, OSError) as error:
        print(f'错误：{error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
