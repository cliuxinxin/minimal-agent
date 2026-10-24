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

构造接口为 `Agent(model, tools, *, system_prompt='', max_steps=80, on_event=None, approve=None, dynamic_tools=None)`。默认每个任务最多调用模型 80 次，`max_steps=800` 支持更长任务，`max_steps=None` 不设请求次数上限。`dynamic_tools` 是返回 `list[Tool]` 的函数，每次模型请求前更新工具快照，不能覆盖静态工具名。连续 8 轮全部工具失败，或连续 8 轮调用名称、参数、结果完全相同时，以 `stalled` 状态停止；结果有变化不会触发相同结果保护。工具调用先经过注册检查、严格参数验证和必要审批，再执行 handler，把成功结果或错误交回模型；最终文本结束任务。

## 观察和核验

`run()` 返回 `RunResult`，状态为 `completed`、`step_limit`、`model_error`、`cancelled` 或 `stalled`，另含 `answer`、`steps`、`tool_calls`、`error`。判断状态后再使用答案；模型结束不等于业务结果正确。`on_event` 可接收运行事件，网页与 CLI 将事件写入 `./runs/*.jsonl`；任务、模型文本和工具结果可能包含业务内容。

后台线程可以用 `agent.run(task, should_stop=stop_event.is_set)` 协作式停止。检查发生在模型调用前、回复后和工具之间；已经开始的网络请求或一般同步 handler 会等到返回或超时。编程工具通过 `miniagent.tools.execution_stopped()` 获取取消状态并终止子进程；你也可以在自定义长任务中检查这个函数。取消时，尚未执行的工具调用也会收到对应的 `cancelled` 错误结果，保证下一轮对话的原生消息协议仍然完整。

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

前端是 `web/` 中的三个静态文件，不需要编译。`miniagent/web.py` 使用标准库 HTTP 服务，默认只监听本机 `127.0.0.1`；它提供配置、会话、消息、停止、文件、工具清单和状态接口。`GET /api/tools` 返回内置工具说明、已保存工具源码与样例，以及无效清单提示。每个会话同一时间只允许一个任务执行，工作线程把 Agent 事件写入日志并供页面轮询。

会话创建时固定服务商、模型、调用上限以及模型/搜索密钥；后续保存设置仅影响新会话，避免把某个提供商的原生历史发送给另一家。网页展示任务状态和工具轨迹，不渲染供应商原始 `model_output` 块。浏览器只接收最近 100 条事件，大参数/结果使用 4000 字符预览；`event_count` 和 `omitted_events` 说明完整数量与省略数量。运行记录抽屉关闭时不重建记录 DOM；完整事件仍保存在本机 JSONL。

聊天状态目前只在进程内保存，配置、上传文件、生成工具与日志保存在磁盘。要增加长期会话，优先在服务端为原生 `agent.history` 和页面消息分别实现持久化，并确保加载时仍匹配创建会话所用的提供商。

运行 `python -m unittest discover -s tests -v` 检查程序边界。新增工具需验证正常输入、错误输入，以及写工具审批拒绝时不执行；模型替身只留在测试里。再用配置的真实模型核对工具轨迹和可独立计算的预期结果。交付未使用你的凭据完成真实 API 联调。
