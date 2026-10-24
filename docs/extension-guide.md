# 扩展指南

## 创建与复用 Agent

先在网页设置中保存模型配置，再从项目根目录使用 Python API。现有 `provider`、`model`、`api_key`、可选 `base_url` 仍可使用，另外可设置 `max_steps` 和 `search_api_key`：

```python
from miniagent.config import load_agent

agent = load_agent("config.json")
result = agent.run("读取 products.md 并列出价格")
print(result.status, result.answer)
```

同一会话复用一个实例；`agent.history` 保留进程内历史，`agent.reset()` 清空历史。网页也为每个会话创建一个独立 Agent。网页与 CLI 的 JSONL 日志用于观察，不承担自动恢复或长期记忆。

## 通过对话创建工具

直接在网页描述工具的输入、期望输出和样例。Agent 会使用 `run_python` 调试代码，再使用 `create_tool` 测试并保存。模型收到的创建参数是：

```json
{
  "name": "normalize_names",
  "description": "清理姓名数组中的前后空格并移除空名字",
  "code": "def run(arguments):\n    return [name.strip() for name in arguments['names'] if name.strip()]\n",
  "example_arguments": {"names": [" Alice ", "", " Bob"]},
  "expected_result": ["Alice", "Bob"],
  "replace_existing": false
}
```

只有实际输出与 `expected_result` 相符才会保存。已有工具默认不覆盖；修改时传 `replace_existing=true`，测试失败保留原版本。不要把一次样例通过理解为工具对全部业务输入都正确，重要边界应补充测试。

工具保存在 `local_tools/normalize_names.json`，包含代码、说明、样例和版本；Git 更新不会覆盖这个被忽略的目录。注册成功后，下一次模型请求的工具列表中就会出现 `normalize_names`，它的调用参数为：

```json
{"arguments": {"names": [" Chen ", " Li"]}}
```

`ProgrammingTools.load_tools()` 只读取和校验清单；只有调用工具时才会在子进程里执行源码。已存在的会话也会在下一次模型请求前加载最新版本。无效清单会跳过并显示在工具面板中，不影响其他有效工具。

`run_python` 的代码按普通脚本运行，支持 `if __name__ == "__main__"`；用 `print()` 输出文本，或者把 JSON 结果赋值给 `result`。保存的工具必须定义 `def run(arguments)` 并返回 JSON 数据。工作目录为 `workspace/`，可以使用标准库及项目 Python 环境里已经安装的包。

每次执行在独立子进程中，默认最多 60 秒、标准输出/错误及结果总量最多 64 KiB；超时、输出超限或点击停止会终止该进程。**这是当前用户权限下的本机 Python，不是操作系统沙箱。** 清理环境变量、独立工作目录和子进程超时不构成文件或网络访问隔离；测试样例也会真实执行源码及其副作用。

## 手动注册工具

参数类继承 `ToolArgs`，默认严格校验并拒绝额外字段。handler 通过关键字参数接收验证后的值；结果应可序列化为 JSON。

```python
from miniagent.tools import Tool, ToolArgs

class AddArgs(ToolArgs):
    a: int
    b: int

def add(a: int, b: int) -> dict:
    return {"sum": a + b}

agent.add_tool(Tool("add", "将两个整数相加", AddArgs, add, read_only=True))
result = agent.run("使用 add 计算 17 加 25")
print(result.answer)
```

为参数写清单位、范围和用途，工具描述说明何时适用。用 Pydantic `Field` 增加约束。不要为接纳模型错误而随意放松验证；错误反馈能让模型修正调用。完整真实 API 示例：`python -m examples.custom_tool '查询 REQ-001 的要求'`。

要让所有网页会话使用自定义工具，在 `miniagent/builtin_tools.py` 的 `build_tools(workspace)` 返回列表里加入对应 `Tool` 即可；`load_agent()` 会统一装配。若某项工具仅用于特定网页会话，可在 `miniagent/web.py` 的 `WebState.new_session()` 中创建 `agent` 后调用 `agent.add_tool(...)`。重启服务后新建会话即可使用。

## 有副作用的工具

`Tool(name, description, args_schema, handler, read_only=False)` 默认需要审批。审批收到工具对象与验证后的参数字典，只有明确返回 `True` 才执行 handler；没有回调或拒绝时均不执行。

```python
def approve(tool, arguments):
    print(tool.name, arguments)
    return input("确认执行？输入 yes: ").strip().lower() == "yes"

agent = load_agent("config.json", approve=approve)
```

将写工具注册到这个实例，保持 `read_only=False`。CLI 已提供交互审批。`read_only` 是工具作者的声明，不会自动检测函数行为；审批之前不要执行实际动作。

当前 `load_agent()` 在未传入审批回调时使用编程工具的宿主策略：只允许该管理器创建的 `run_python`、`create_tool` 和已加载工具对象。它们仍然标记为 `read_only=False`，不会冒充只读。其他写工具默认不被这份策略允许；有需要时由开发者扩展明确的授权范围。CLI 提供逐次输入确认；网页线程不能直接使用 `input()`。

## 模型与循环

`OpenAIModel(model, api_key, base_url=None)` 使用 Chat Completions 的 `tool_calls`；`AnthropicModel` 采用相同构造参数，使用 Messages 的 `tool_use`。两者位于 `miniagent.model`，负责把原生工具调用及工具结果接入循环，无需让模型输出自定义 JSON 动作协议。

需要直接装配时：

```python
from pathlib import Path
from miniagent.agent import Agent
from miniagent.builtin_tools import build_tools
from miniagent.model import OpenAIModel

model = OpenAIModel(model="model-name", api_key="your-api-key")
agent = Agent(model, build_tools(Path("workspace")), max_steps=80)
```

构造接口为 `Agent(model, tools, *, system_prompt='', max_steps=80, on_event=None, approve=None, dynamic_tools=None, stream=False)`。默认每个任务最多调用模型 80 次，`max_steps=800` 支持更长任务，`max_steps=None` 不设请求次数上限。`dynamic_tools` 是返回 `list[Tool]` 的函数，每次模型请求前更新工具快照，不能覆盖静态工具名。连续 8 轮全部工具失败，或连续 8 轮调用名称、参数、结果完全相同时，以 `stalled` 状态停止；结果有变化不会触发相同结果保护。工具调用先经过注册检查、严格参数验证和必要审批，再执行 handler，把成功结果或错误交回模型；最终文本结束任务。

## 观察和核验

`run()` 返回 `RunResult`，状态为 `completed`、`step_limit`、`model_error`、`cancelled` 或 `stalled`，另含 `answer`、`steps`、`tool_calls`、`error`。判断状态后再使用答案；模型结束不等于业务结果正确。`on_event` 可接收运行事件，网页与 CLI 将事件写入 `./runs/*.jsonl`；任务、模型文本和工具结果可能包含业务内容。

后台线程可以用 `agent.run(task, should_stop=stop_event.is_set)` 协作式停止。检查发生在模型调用前、回复后和工具之间；流式模型传输会主动关闭正在等待数据的连接。非流式请求、搜索及一般同步 handler 会等到返回或超时。编程工具通过 `miniagent.tools.execution_stopped()` 获取取消状态并终止子进程；你也可以在自定义长任务中检查这个函数。已经完整接收的工具调用会收到对应的 `cancelled` 错误结果；未完整接收的模型回复不加入原生历史，也不执行工具。

### 接收模型增量

网页默认开启 `stream=True`，Python API 和 CLI 保留非流式默认值。使用 `load_agent(..., stream=True, on_event=handler)` 即可接收模型增量，不增加配置文件字段：

```python
def observe(event):
    if event["event"] == "text_delta":
        print(event["data"]["text"], end="", flush=True)

agent = load_agent("config.json", stream=True, on_event=observe)
result = agent.run("读取 products.md 并总结")
```

新模型适配器可实现 `stream(messages, tools, system_prompt, *, on_delta, should_stop=None) -> ModelReply`。它在接收数据时调用 `on_delta`，在验证正常终止后返回完整 `ModelReply`。增量类型为 `text_delta`、`reasoning_delta` 与 `tool_call_delta`；工具增量含 `index`、累计的 `id` / `name` 和本次 `arguments_delta`。工具参数只能在完整回复通过验证后执行。

核心事件仍含 `run_id`、`event`、`step`、`time`、`data`。`model_complete` 表示单次模型回复结束，其 `data` 含 `text` 和 `has_tool_calls`；随后才可能出现 `tool_call` / `tool_result`。`model_output` 含原生消息，只用于本地内部日志，不发送给网页。只实现 `complete()` 的自定义模型仍可运行，公开文本会一次送达，不模拟逐字流式。

内置只读文件工具是 `list_files` 和 `read_file`，范围固定在 `workspace/`；Python API 可通过 `load_agent(..., workspace=Path(...))` 指定工作目录。编程工具具有当前用户的 Python 权限。自定义工具应自行约束访问范围、设置超时，并避免把凭据传给模型。

## DeepSeek 搜索与原文

在网页设置填写搜索 Key，`load_agent()` 就会注册 `web_search(query, count=5)` 与 `fetch_webpage(url, offset=0, limit=6000)`。它们独立于主模型适配器，OpenAI 或 Anthropic 均可调用。

- Search：`POST https://api.deepseek.com/v1/tools/search`，发送 `query`、`count`、`need_content=false`。
- Fetch：`POST https://api.deepseek.com/v1/tools/fetch`，发送 `url`。
- 两者使用 `Authorization: Bearer <DeepSeek API Key>`，地址固定为官方服务。
- 搜索 query 为 1–128 字符；本项目每次允许 1–10 条结果，优先保留完整来源 URL。
- 原文按字符分段，使用 `next_offset` 继续读取。HTTP 响应最多 2 MiB，工具给模型的单次结果保持在核心结果预算内。
- 网络超时为 30 秒，拒绝重定向，不自动重试。模型会获得经过脱敏的实际错误，并决定后续动作。

来源：[DeepSeek Search / Fetch 官方指南](https://api-docs.deepseek.com/guides/search/)。搜索和页面正文属于外部数据，系统提示要求模型使用实际返回的 URL 引用来源，不执行网页中的指令。

## 前端与服务端扩展

运行时前端在 `web/` 中，仓库提供预构建文件，普通用户不需要编译。对话直接使用 assistant-ui 的 React 组件与 Markdown 渲染器，开发源码和固定依赖位于 `frontend/`。现有 EventSource 状态通过 `ExternalStoreRuntime` 转成 text、reasoning 和 tool-call 消息片段；工具仍只在 Python 服务端执行，不启用前端工具执行器。`miniagent/web.py` 使用标准库 HTTP 服务，默认只监听本机 `127.0.0.1`；它提供配置、会话、消息、停止、文件、工具清单和 SSE 接口。`GET /api/tools` 返回内置工具说明、已保存工具源码与样例，以及无效清单提示。每个会话同一时间只允许一个任务执行，工作线程独立运行并向页面推送 SSE。

会话创建时固定服务商、模型、调用上限以及模型/搜索密钥；后续保存设置仅影响新会话，避免把某个提供商的原生历史发送给另一家。网页展示实际文本、思考摘要和工具结果，不发送供应商原始 `model_output` 块、思考签名或加密内容。对话中的同一 assistant 消息从运行开始一直更新到结束；片段有稳定 ID，保留最近 80 个，每个文字/思考片段最多 64 KiB，省略时显示提示。记录抽屉保留最近 100 条粗粒度事件，大参数/结果使用 4000 字符预览；增量文字不挤占记录数量。完整事件仍保存在本机 JSONL。

`GET /api/sessions/{id}/events` 接受 `Last-Event-ID` 或 `?after=<序号>`，以每个会话递增的 ID 推送事件：

| SSE 事件 | 数据与用途 |
| --- | --- |
| `snapshot` | `{session}`；首次连接、恢复或结束时的权威状态 |
| `run_status` | `{run_id, status, step, error, event_count, omitted_events}` |
| `segment` | `{run_id, segment, segments_omitted}`；按稳定 ID 更新完整片段 |
| `segment_delta` | `{run_id, segment_id, text}`；追加本次公开文字 |
| `trace` | `{run_id, event, event_count, omitted_events}`；更新记录抽屉 |

服务端保留最近 512 个 SSE 事件，游标太旧时返回当前快照；心跳注释保持连接。重连只恢复观察状态，不能触发模型或工具重新执行。前端切换会话时关闭旧 EventSource，并核对会话版本，防止旧连接污染新对话。流式密钥清理保留可能跨分片的密钥前缀，完整识别后再发布。

每轮新任务开始时释放前一轮的重放缓存，保持会话序号递增；旧轮游标强制获得新快照。终态 `message.content` 保留完整最终答案，前端可据此恢复最终文字，或在无法可靠还原多段边界时提供完整原文与复制入口。运行中的预览限制不应造成最终答案静默丢失。停止请求的异步响应同时核对会话、版本和运行 ID，避免旧请求影响新任务。

聊天状态目前只在进程内保存，配置、上传文件、生成工具与日志保存在磁盘。要增加长期会话，优先在服务端为原生 `agent.history` 和页面消息分别实现持久化，并确保加载时仍匹配创建会话所用的提供商。

运行 `python -m unittest discover -s tests -v` 检查程序边界。新增工具需验证正常输入、错误输入，以及写工具审批拒绝时不执行；模型替身只留在测试里。再用配置的真实模型核对工具轨迹和可独立计算的预期结果。交付未使用你的凭据完成真实 API 联调。
