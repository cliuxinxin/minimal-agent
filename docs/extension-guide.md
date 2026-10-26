# 扩展指南

## 创建与复用 Agent

先在网页设置中保存模型配置，再从项目根目录使用 Python API。现有 `provider`、`model`、`api_key`、可选 `base_url` 仍可使用，另外可设置 `max_steps` 和 `search_api_key`：

```python
from miniagent.config import load_agent

agent = load_agent("config.json")
result = agent.run("读取 products.md 并列出价格")
print(result.status, result.answer)
```

同一会话复用一个实例；`agent.history` 是当前模型请求历史，`history_archive` 保留未压缩的原始历史，`context_summary` 与 `compaction_count` 保存工作摘要状态。`agent.reset()` 清空这些状态。网页通过本地 SQLite 恢复它们；CLI 的持久化仍由调用者决定。JSONL 日志用于观察。

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

`run()` 返回 `RunResult`，状态为 `completed`、`step_limit`、`model_error`、`cancelled` 或 `stalled`，另含 `answer`、`steps`、`tool_calls`、`error`，以及可选的 `diagnostic` 和 `metrics`。新增字段有默认值，原有调用方式继续有效。判断状态后再使用答案；模型结束不等于业务结果正确。`on_event` 可接收运行事件，网页与 CLI 将事件写入 `./runs/*.jsonl`；任务、模型文本和工具结果可能包含业务内容。

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

模型适配器可通过 `ModelError(message, diagnostic={...})` 提供结构化故障信息。允许字段包括 `code`、`phase`、`field`、`expected`、`actual_type`、`chunk_index`、`event_type`、`finish_reason`、`http_status`、`request_id`、`elapsed_ms` 和只含类型/长度的 `chunk_shape`。不要将原始响应、请求头、提示词或工具参数放入诊断字段。Agent 将诊断附在 `RunResult.diagnostic` 和 `run_end`；异常的代码位置只记录文件名、函数名和行号，不记录源码、局部变量或完整路径。

网页宿主会为失败生成 `schema_version=1` 的报告，补充版本、会话/运行编号、连接信息、工具状态和最近阶段事件。报告附在失败的 assistant 消息与当前 run 的 `diagnostic` 上，随终态 SSE `snapshot` 到达前端；开始下一轮不覆盖旧消息的报告。同一份报告原子写入 `runs/<run_id>.diagnostic.json`。写入失败时保留页面报告，`diagnostic_file=null`，`storage_error` 仅含异常类型。

流式工具片段的 `status` 为 `preparing`、`running`、`completed`、`error`、`cancelled`、`not_executed` 或 `interrupted`。`not_executed` 表示未进入工具执行，`interrupted` 表示已开始但没有收到结果；缺少 `result` 和真实结果为 JSON `null` 是不同状态，前端不能将两者混为成功。

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

`GET /api/health` 返回启动时固定的运行标识：`app`、`version`、`workspace_id`、`source_revision`；`/api/bootstrap.runtime` 和失败报告中的 `runtime` 使用同一份标识。`source_revision` 是应用 Python 源码和启动器的 SHA-256 指纹，不包含用户配置、工作文件、生成工具或日志。同步新文件不会让旧进程的标识变成新版。

双击启动器仅在工作目录、版本、代码指纹均匹配时复用服务，否则启动新进程；默认端口被占用时沿用自动选择空闲端口的行为。前端版本由构建时的 `frontend/package.json` 注入，启动时读取 bootstrap 的运行标识，旧服务缺少该字段时读取 health。版本不匹配时提示重新启动，保留草稿、查看旧对话和停止运行，只暂停发送新任务；不自动重放模型或工具请求。

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

## v0.5.0 本地工作台接口

`miniagent/persistence.py` 用 SQLite 事务保存页面消息和四个 Agent 状态字段。配置快照只包含 provider/model/base_url/max_steps/search_enabled，不包含凭据。恢复时先检查同一提供商和 endpoint，再从当前配置取得密钥。未配对的原生工具调用补上 `interrupted_unknown_outcome` 结果，绝不据此自动执行工具。接收任务到工作线程启动之间用 `pending_task` 保存目标；原生 user 检查点提交后清除。每份会话有递增 revision，过期进程的保存会被拒绝。

| 接口 | 行为 |
| --- | --- |
| `POST /api/check-connection` | `{target:'model'或'search', config:{设置字段}}`；按需验证草稿设置，返回 checks；不保存配置、不创建会话 |
| `POST /api/sessions/{id}/continue` | `{}`；用户请求继续中断／失败／已停止的任务 |
| `GET /api/artifacts/{id}` | 返回快照元信息、文本预览、可选 CSV 表格及 diff |
| `GET /api/artifacts/{id}/download` | 以附件返回已归档的固定字节，不读取任意工作区路径 |
| `POST /api/tools/{name}/enabled` | `{enabled:bool}`；下次动态工具刷新生效 |
| `POST /api/tools/{name}/rollback` | `{}`；交换当前与上一版本，保留启停状态，不执行代码 |
| `POST /api/tools/{name}/test` | `{}`；在锁外测试所读取的固定版本，返回 version/total/results/passed |

assistant 消息可包含 `artifacts`、`sources`、`metrics`。来源由实际搜索／抓取结果产生：`{id,title,url,date,read}`；模型使用 `[source_id](真实URL)`。UI 校验 ID 与 URL 是否匹配，排除代码中的引用示例，不把链接匹配视为事实证明。抓取缓存使用 `execution_run_id()` 区分任务，跨任务不复用。

`ModelReply.usage` 是可选的归一化 input_tokens/output_tokens/total_tokens；`RunResult.metrics` 汇总实际已观测用量、known_usage_calls、模型／工具次数、时间和压缩次数。OpenAI 仅对官方 HTTPS 域请求 include_usage，其他兼容接口只消费实际返回的 usage；Anthropic 合并消息起始和累计输出用量。缺失字段保持 `None`，不伪造精确用量或费用。

`checkpoint` 是内部事件，不把原生历史放进 SSE；宿主在这个边界读取 Agent 状态并持久化。`usage` 和 `context_compaction` 提供可显示的状态。`run_status` 额外带 metrics/phase/last_activity_at/finished_at/storage_warning。前端仍不执行任何模型工具。

create_tool 保留原有 example_arguments/expected_result，新增可选 `tests`（最多 20 组附加样例）与 `input_schema`。支持对象字段、required/additionalProperties、数组 items、基本类型、enum、字符串／数组／数值边界；不支持的 schema 关键字会拒绝。所有样例通过后才发布新版本。单样例通过只证明该样例，不代表程序全面正确。

运行 `python -m unittest discover -s tests -v` 检查程序边界。新增工具需验证正常输入、错误输入，以及写工具审批拒绝时不执行；模型替身只留在测试里。再用配置的真实模型核对工具轨迹和可独立计算的预期结果。交付未使用你的凭据完成真实 API 联调。

## v0.6.0 公开计划与运行中纠偏

应用装配通过 `Agent(..., planning=True)` 注册 `update_work_plan`。直接构造 `Agent` 的默认值仍是 `False`，便于扩展者决定自己的工具集合；网页配置不增加开关。`miniagent/planning.py` 定义严格、有界的 `WorkPlanArgs`：goal、success_criteria、steps、summary、evidence、uncertainties、verification。revision 和 updated_at 由宿主分配；输入完全相同时保持版本不变，防止反复改写同一计划绕过原有空转保护。

`Agent.work_plan` 保存最新计划，后续请求把它作为可能过时的工作数据注入上下文，最新用户要求优先。`work_plan` 事件的 `data.plan` 更新公开界面；内部 `checkpoint` 保证恢复。页面和每条 assistant 消息分别保存当前状态与该轮快照。模型摘要是面向用户的判断说明，verification 是模型自检；不把这些字段视为工具执行成功或业务正确性的证明。

`POST /api/sessions/{id}/steer` 接收 `{run_id, input_id, message}`，成功返回 202 和 `{input, session}`。input_id 为 1–64 个字母、数字、下划线或连字符；message 为 1–8000 字符。同一 ID、run 和原文重试保持幂等，复用 ID 修改内容返回 409。过期、停止、其他后台占用、已关闭输入的运行返回 409；最多 10 条待应用输入、每轮 100 条已接收输入，超出返回 429。存储失败返回 503，不能将内容加入可执行队列后误报成功。

`detail.steering` 和消息内 steering 使用 `{id, run_id, message, status: pending|applied, created_at, applied_at?}`。原始队列单独持久化；公开文字在接收时脱敏并保留，恢复时不会因原密钥已不可用而重新暴露原文。HTTP 只排队，不能修改原生模型历史。

Runner 的 `run(task, should_stop=..., pending_input=...)` 接受 `pending_input(final=False)` 回调，返回 `{id,message,before_task?}`。读取队列不确认消费。runner 在安全边界追加原生用户消息，随后更新 `applied_input_ids`、发出 `input_applied` 和检查点。网页 Agent 的 `state_lock` 与 WebState 使用同一个 RLock；历史、归档、已应用 ID 和确认状态在同一锁内提交，网络与工具执行在锁外。

纠偏在模型请求前、模型返回后和每个工具执行前检查。正在执行的工具完成后，剩余旧方向调用获得 `superseded_by_user` 错误结果，页面显示未执行；所有原生调用／结果配对完整后才插入用户消息。最终文本接受前调用 `pending_input(final=True)`：宿主在同一锁内返回待处理输入，或设置 `run.accepting_input=false` 关闭接收，避免结束瞬间丢补充。

停止优先于纠偏；未应用输入保留，下一次明确继续时进入上下文。前一轮的 pending 输入先于更新的用户任务加入历史。若新任务已被接受但工作线程尚未启动就退出，恢复会先补旧纠偏，再补新任务，以维持指令先后顺序。`applied_input_ids` 保留最近 512 个 ID，持久化记录的 applied 状态也阻止旧输入被重复消费。

正文来源编号是前端呈现：内部 ID、原始消息和 URL 保持原样。转换要求 source ID 与 URL 都匹配，复制时使用 Markdown AST 的标签位置，不替换代码、URL 或普通文字。当前计划中的已知来源也呈现为编号及可读标题。无匹配来源只能提示待核对。
