# 扩展指南

## 创建与复用 Agent

先在网页设置中保存模型配置，再从项目根目录使用 Python API。配置只有 `provider`、`model`、`api_key`、可选 `base_url`：

```python
from miniagent.config import load_agent

agent = load_agent("config.json")
result = agent.run("读取 products.md 并列出价格")
print(result.status, result.answer)
```

同一会话复用一个实例；`agent.history` 保留进程内历史，`agent.reset()` 清空历史。网页也为每个会话创建一个独立 Agent。网页与 CLI 的 JSONL 日志用于观察，不承担自动恢复或长期记忆。

## 注册工具

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

网页目前只提供内置只读工具，未实现写操作确认界面，因此网页装配时使用 `approve=None`，默认拒绝写工具。若扩展写操作，需要先添加浏览器确认及服务端审批衔接，不能把上面的 `input()` 回调直接用于网页请求线程。

## 模型与循环

`OpenAIModel(model, api_key, base_url=None)` 使用 Chat Completions 的 `tool_calls`；`AnthropicModel` 采用相同构造参数，使用 Messages 的 `tool_use`。两者位于 `miniagent.model`，负责把原生工具调用及工具结果接入循环，无需让模型输出自定义 JSON 动作协议。

需要直接装配时：

```python
from pathlib import Path
from miniagent.agent import Agent
from miniagent.builtin_tools import build_tools
from miniagent.model import OpenAIModel

model = OpenAIModel(model="model-name", api_key="your-api-key")
agent = Agent(model, build_tools(Path("workspace")), max_steps=8)
```

构造接口为 `Agent(model, tools, system_prompt='', max_steps=8, on_event=None, approve=None)`。默认每个任务最多调用模型 8 次。工具调用先经过注册检查、严格参数验证和必要审批，再执行 handler，把成功结果或错误交回模型；最终文本结束任务。

## 观察和核验

`run()` 返回 `RunResult`，状态为 `completed`、`step_limit`、`model_error` 或 `cancelled`，另含 `answer`、`steps`、`tool_calls`、`error`。判断状态后再使用答案；模型结束不等于业务结果正确。`on_event` 可接收运行事件，网页与 CLI 将事件写入 `./runs/*.jsonl`；任务、模型文本和工具结果可能包含业务内容。

后台线程可以用 `agent.run(task, should_stop=stop_event.is_set)` 协作式停止。检查发生在模型调用前、回复后和工具之间；已经开始的网络请求或同步 handler 会等到返回或超时。取消时，尚未执行的工具调用也会收到对应的 `cancelled` 错误结果，保证下一轮对话的原生消息协议仍然完整。

内置工具 `list_files`、`read_file`、`calculate` 均为只读。网页的文件范围固定在项目目录的 `workspace/`；Python API 可通过 `load_agent(..., workspace=Path(...))` 指定目录。路径边界不是操作系统沙箱。自定义工具应自行约束访问范围、设置超时，并避免把凭据传给模型。

## 前端与服务端扩展

前端是 `web/` 中的三个静态文件，不需要编译。`miniagent/web.py` 使用标准库 HTTP 服务，默认只监听本机 `127.0.0.1`；它提供配置、会话、消息、停止、文件和状态接口。每个会话同一时间只允许一个任务执行，工作线程把 Agent 事件写入日志并供页面轮询。

会话创建时固定服务商、模型和密钥；后续保存设置仅影响新会话，避免把某个提供商的原生历史发送给另一家。网页展示任务状态和工具轨迹，不渲染供应商原始 `model_output` 块。

聊天状态目前只在进程内保存，上传文件与日志保存在磁盘。要增加长期会话，优先在服务端为原生 `agent.history` 和页面消息分别实现持久化，并确保加载时仍匹配创建会话所用的提供商。

运行 `python -m unittest discover -s tests -v` 检查程序边界。新增工具需验证正常输入、错误输入，以及写工具审批拒绝时不执行；模型替身只留在测试里。再用配置的真实模型核对工具轨迹和可独立计算的预期结果。交付未使用你的凭据完成真实 API 联调。
