"""Explicit, bounded checks using real provider adapters and a harmless echo."""
import json
import re
import threading
import time

from .diagnostics import diagnostic_fields
from .search import DeepSeekSearch


def classify_error(error):
    diagnostic = diagnostic_fields(getattr(error, 'diagnostic', None))
    status = diagnostic.get('http_status')
    if status is None:
        match = re.search(r'HTTP\s+(\d{3})', str(error))
        status = int(match.group(1)) if match else None
    descriptions = {
        401: ('authentication', '密钥未通过验证，请检查 API Key。'),
        402: ('billing', '接口提示额度不足，请检查账户余额或账单。'),
        403: ('permission', '接口拒绝访问，请检查模型权限和账户限制。'),
        404: ('not_found', '未找到模型或接口，请检查模型名称与 API 地址。'),
        408: ('timeout', '接口请求超时，请检查网络后重试。'),
        429: ('rate_limit', '接口限流或额度受限，请稍后重试并检查账户配额。'),
    }
    if status in descriptions:
        return descriptions[status]
    if isinstance(status, int) and status >= 500:
        return 'provider_unavailable', '模型服务暂时不可用，请稍后重试。'
    if status == 400:
        return 'request_rejected', '接口拒绝了标准请求，请检查模型和工具调用支持情况。'
    if diagnostic.get('phase') in {'stream_delta', 'stream_event', 'stream_finish'}:
        return 'stream_format', '接口流式返回格式不兼容，请复制运行诊断检查具体字段。'
    if isinstance(error, (TimeoutError, OSError)) or any(word in str(error).lower() for word in ('timed out', 'timeout', 'request failed')):
        return 'network', '无法完成网络请求，请检查接口地址、网络和代理连接。'
    return 'check_failed', '连接检查未完成，请确认接口支持标准流式和工具调用。'


def check_connection(model, *, target, search_key=None) -> dict:
    started, checks = time.monotonic(), []
    stop = threading.Event()
    timer = threading.Timer(45, stop.set)
    timer.daemon = True
    timer.start()

    def add(name, status, message, code=None):
        item = {'name': name, 'status': status, 'message': message}
        if code:
            item['code'] = code
        checks.append(item)

    try:
        if target == 'search':
            if not search_key:
                add('搜索连接', 'failed', '请填写 DeepSeek 搜索 API Key。', 'search_not_configured')
            else:
                DeepSeekSearch(search_key).search('DeepSeek API documentation', count=1)
                add('搜索连接', 'passed', '搜索接口已返回有效结果。')
        else:
            echo = {'name': 'connection_echo', 'description': 'Echo a connection-check value without side effects.',
                    'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}},
                                   'required': ['value'], 'additionalProperties': False}}
            prompt = ('This is a connection check. Call connection_echo exactly once with '
                      'value="minimal-agent-check". After receiving its result, reply with OK and no tools.')
            messages = [{'role': 'user', 'content': 'Check the connection and tool round trip.'}]
            reply = model.stream(messages, [echo], prompt, on_delta=lambda delta: None, should_stop=stop.is_set)
            add('流式连接', 'passed', '已收到完整且有效的模型流式回复。')
            if (len(reply.tool_calls) != 1 or reply.tool_calls[0].name != 'connection_echo'):
                add('工具调用', 'failed', '模型没有返回要求的测试工具调用，本次无法确认兼容性。', 'tool_call_unverified')
            else:
                call = reply.tool_calls[0]
                try:
                    arguments = json.loads(call.arguments)
                except (ValueError, TypeError):
                    arguments = None
                if arguments != {'value': 'minimal-agent-check'}:
                    add('工具参数', 'failed', '模型返回的测试工具参数不符合约定。', 'tool_arguments')
                else:
                    add('工具调用', 'passed', '工具名称和参数有效；仅在内存中执行无副作用的回显。')
                    messages.append(reply.message)
                    messages.extend(model.tool_messages([{'id': call.id, 'name': call.name,
                                                         'payload': {'ok': True, 'result': arguments['value']}}]))
                    reply = model.stream(messages, [], prompt, on_delta=lambda delta: None, should_stop=stop.is_set)
                    if reply.tool_calls or not reply.text.strip():
                        add('结果续接', 'failed', '模型收到工具结果后未返回最终文本。', 'tool_continuation')
                    else:
                        add('结果续接', 'passed', '模型已接收工具结果并完成回答。')
    except Exception as error:
        code, message = classify_error(error)
        if stop.is_set():
            code, message = 'timeout', '连接检查超时，请检查网络后重试。'
        add('搜索连接' if target == 'search' else '模型连接', 'failed', message, code)
    finally:
        timer.cancel()
    return {'ok': bool(checks) and all(item['status'] == 'passed' for item in checks),
            'target': target, 'checks': checks, 'elapsed_seconds': round(time.monotonic() - started, 2)}
