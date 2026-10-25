# Minimal Agent

一个能在桌面浏览器里聊天、编程、创建可复用工具、搜索网页、上传文本文件并实时查看执行过程的 Python Agent。文字、模型提供的思考内容和工具执行过程通过 SSE 出现在对话中。核心循环保持明确，方便阅读和扩展；界面与命令行共用同一个 Agent。

支持标准 **OpenAI Chat Completions** 和 **Anthropic Messages**，使用各自原生的 `tool_calls` / `tool_use`。应用调用你配置的真实 API；没有离线演示模式。

## 打开使用

1. 安装 **Python 3.11 或更新版本**。Windows 安装时勾选 **Add python.exe to PATH**。
2. 从 GitHub 同步项目到本机，或把整个压缩包解压到一个可以写入的文件夹；别直接在压缩包里打开文件。
3. Windows 双击 **Start.bat**；macOS 双击 **Start.command**。第一次会自动创建项目专用的 Python 环境并安装 Pydantic，需要联网，可能要等一小会儿。
4. 浏览器自动打开后，在模型设置中选择 **OpenAI** 或 **Anthropic**，填写 **模型名称**、**API Key** 并保存。接口地址可留空，使用官方默认地址。
5. 如需联网搜索，在同一设置窗口填写 **DeepSeek 搜索 API Key**。搜索使用独立的官方接口，主模型仍可以是 OpenAI 或 Anthropic。
6. 输入任务并发送。正常使用都在网页里完成，不需要输入命令。

启动窗口会保持打开，为网页提供本地服务；关闭它会停止服务。浏览器默认地址为 `http://127.0.0.1:8765`，端口被占用时会自动选择其他端口并打开正确地址。macOS 如果首次阻止打开脚本，可在 Finder 里右键文件，选择“打开”。

运行环境只有 Python 和 Pydantic；对话界面使用成熟的 **assistant-ui** React 组件，构建好的前端文件随项目提供。正常使用无须安装 Node.js、运行前端构建或连接在线 CDN。

后续更新时，关闭启动窗口、同步 GitHub 的最新代码，再双击启动即可。`.gitignore` 会排除本机配置、虚拟环境、运行日志、新增的上传文件和 `local_tools/` 中生成的工具；仓库仅保留两份随项目提供的示例文本。

Start 会检查已运行服务的版本和代码指纹。只有与当前文件一致时才复用；发现旧服务时会启动新版服务，并自动打开它的页面。如果旧服务仍占用默认端口，新版会选择空闲端口。请使用这次自动打开的页面；刷新旧标签页仍然连接原来的进程。旧启动窗口可以在任务结束后关闭。

页面会显示当前服务版本。发现网页与后台版本不一致时，会提示重新双击 Start，并暂停发送新任务；已输入的草稿、旧对话和正在运行任务的停止按钮仍保留。“重新检查”用于再次读取服务版本，“复制运行诊断”只复制版本与代码标识，不包含配置、Key 或对话内容。

## 网页里能做什么

- **聊天与新建对话**：同一对话保留上下文，可以在侧栏切换会话。
- **上传文件**：支持 UTF-8 编码的 `.txt`、`.md`、`.json`、`.csv`，每个最多 2 MiB。文件保存在项目的 `workspace/`，同名文件自动改名。
- **实时对话**：模型开启原生流式输出，本地服务通过 SSE 推送到页面。回答逐步显示；工具准备、执行和结果按顺序出现在同一条对话中，不必打开记录抽屉。
- **思考内容**：接口实际返回的公开思考内容或摘要显示在可展开区域。兼容 OpenAI 格式的 `reasoning_content` 和 Anthropic 的 `thinking_delta`；没有返回内容时只显示运行状态，不生成假的思考过程。签名和加密思考块不会发送到页面。
- **Markdown**：支持标题、粗体、引用、嵌套列表、表格、任务列表、删除线、代码高亮与复制。流式过程中会处理尚未闭合的格式，完成后按原文渲染。
- **查看执行过程**：对话保留最近 80 个文字、思考或工具片段，长文字片段显示截断提示；记录抽屉保留最近 100 条阶段事件。完整运行事件保存在本机 `runs/`。
- **故障诊断**：失败消息下方可展开“诊断信息”，复制或下载 JSON 报告。报告记录运行编号、模型与接口、失败阶段、错误字段和类型、流片段编号，以及接口提供的请求编号；同一份报告自动保存在本机 `runs/`。
- **编程**：`run_python` 在本机 Python 子进程中运行代码，可读取工作文件、生成结果、执行计算和处理文本。原来的 Calculator 已移除。
- **创建工具**：`create_tool` 运行样例测试，结果与预期相符才保存。下一次模型请求就能使用新工具；新对话和重启后也能使用。点击网页“工具”可查看已保存的代码和样例。
- **联网搜索**：`web_search` 搜索网页并返回真实来源链接；`fetch_webpage` 获取网页正文并支持分段读取。填入 DeepSeek 搜索 Key 后启用。
- **停止运行**：停止后中断正在读取的模型流和 Python 子进程，不再发起下一次模型或工具调用；已显示的内容保留并标记停止。搜索请求和一般同步工具仍需等待返回或超时；搜索超时为 30 秒。
- **断线恢复**：页面与服务之间短暂断开时自动重连，补齐事件或恢复当前快照，不会重新执行任务。关闭网页不会停止后台任务；需要停止时点击停止按钮。
- **任务上限**：默认 80 次模型请求，网页可选择 800 次或“不限制次数”。连续 8 轮工具全部失败，或连续 8 轮调用参数和结果完全相同，程序会停止并说明原因。
- **模型设置**：保留服务商、模型、API Key、可选接口地址，新增调用次数上限和可选搜索 Key。保存设置后新建对话使用新配置；已有对话保留创建时的配置。

可以直接发送：

> 帮我写一个工具，把订单数组按月份汇总金额。先用两个月的样例验证，测试通过后保存为 monthly_totals，接着用这个工具处理我上传的数据。

也可以让它先运行一段程序：

> 读取 products.md，用 Python 计算键盘和鼠标打九折后的总价。

配置搜索 Key 后：

> 搜索 DeepSeek 搜索接口的最新官方说明，打开原文，整理要点并附上来源链接。

Python 的当前工作目录是 `workspace/`。代码使用已安装的 Python 环境和标准库；应用不会自动安装第三方包。每次执行最多 60 秒、输出最多 64 KiB。**Python 按当前用户权限在本机运行，不是操作系统沙箱**；它并不因为当前目录是 `workspace/` 就无法访问其他文件。为生成的工具选择合适的输入与副作用范围，测试代码也会实际执行。

## 配置和会话

网页设置保存在项目的 `config.json`，无需手工编辑。现有四项配置文件可直接继续使用；新增的 `max_steps` 默认为 80，`search_api_key` 默认为未配置。网页选择“不限制次数”会把 `max_steps` 保存为 `null`。模型名称应填写该服务实际支持的名称。

| 服务商 | 标准接口 | 默认基础地址 |
| --- | --- | --- |
| `openai` | Chat Completions | `https://api.openai.com/v1` |
| `anthropic` | Messages | `https://api.anthropic.com` |

API Key 不写入浏览器存储，读取配置时服务端只返回“是否已设置”；它以明文保存在本机 `config.json`，请勿把这个文件提交或分享。保持服务商和接口地址不变时，编辑设置可把密钥留空以沿用现有值；更改服务商或接口地址时需要重新填写密钥。

DeepSeek 搜索使用 `POST https://api.deepseek.com/v1/tools/search`，正文抓取使用 `POST https://api.deepseek.com/v1/tools/fetch`，认证均为普通 DeepSeek API Key 的 Bearer 认证。无需修改主模型或另外配置搜索地址。接口依据 [DeepSeek 官方搜索与抓取指南](https://api-docs.deepseek.com/zh-cn/guides/search/)。搜索 Key 同样只保存在本机，留空保留已存值，点击清除并保存才会移除；移除对新对话生效。

已生成的工具保存在 `local_tools/<工具名>.json`，包含 Python 源码、说明、测试样例和版本。该目录已加入 Git 忽略规则；正常在原目录更新代码会保留。通过 `create_tool` 修改工具需要显式指定 `replace_existing=true`，新代码测试失败会保留旧版本。

对话保存在本地服务的内存中：刷新网页会保留，关闭服务会清空。配置、上传文件和 `runs/*.jsonl` 执行日志保存在磁盘中；日志用于检查运行过程，不会自动恢复会话。日志可能含任务、文件和工具结果，请按原始业务数据管理。

## 流式输出与思考显示

网页默认使用流式接口，无需增加配置。OpenAI 适配器使用 `POST /chat/completions` 的 `stream=true`；Anthropic 适配器使用 `POST /v1/messages` 的 `stream=true`。工具参数在完整、有效的回复结束后才执行，未完成的参数不会触发工具。

思考内容取决于模型和接口能力。标准 OpenAI Chat Completions 没有通用的可见推理摘要字段；OpenAI 的推理摘要属于 Responses API，需要显式请求，本项目继续使用 Chat Completions。Anthropic 只有在接口实际返回可见 `thinking_delta` 时才展示。不同 Claude 版本的思考参数并不通用，本项目不会自动注入模型可能不支持的参数。使用 DeepSeek 等兼容接口时，如果服务返回 `reasoning_content`，会实时显示并按原生协议保留在后续上下文中。

参考：[OpenAI reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning#reasoning-summaries)、[Anthropic streaming](https://platform.claude.com/docs/en/build-with-claude/streaming)、[DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)。

对话显示直接使用 [assistant-ui](https://www.assistant-ui.com/) 的组件与 Markdown 渲染方案。`ExternalStoreRuntime` 负责把现有 Python/SSE 会话接入组件；消息列表、自动滚动、思考区域和工具调用沿用库的能力。源码、固定依赖与组件来源在 `frontend/`，已构建的浏览器文件在 `web/assets/`。Markdown 不执行模型返回的 HTML，危险链接不会成为可点击地址，外部图片不会自动加载。

运行中的长内容保持有界预览，截断时显示说明；完成后保留完整最终答案，必要时可展开原文并复制完整回复。开发者修改对话组件和重新构建的方法见 [前端开发说明](frontend/README.md)。

## 读懂并扩展

| 文件 | 职责 |
| --- | --- |
| `web/index.html`、`web/style.css`、`web/app.js` | 桌面界面、设置、SSE 会话状态 |
| `frontend/`、`web/assets/` | assistant-ui 源码、依赖锁定与已构建的对话组件 |
| `miniagent/web.py` | 本地 HTTP/SSE 接口、会话、上传、后台运行和日志 |
| `miniagent/agent.py` | 模型调用 → 工具选择 → 验证和审批 → 执行 → 回传结果 |
| `miniagent/model.py`、`miniagent/sse.py` | 两家服务的原生流式适配器与有界 SSE 传输 |
| `miniagent/diagnostics.py` | 有界诊断字段、异常位置和可分享报告的本地保存 |
| `miniagent/runtime.py` | 已运行服务的版本与代码标识，供启动器和网页核对 |
| `miniagent/tools.py` | `ToolArgs` 严格参数验证和 `Tool` 执行边界 |
| `miniagent/builtin_tools.py` | `list_files`、`read_file` |
| `miniagent/programming.py`、`miniagent/python_worker.py` | Python 子进程、样例测试、工具保存与动态加载 |
| `miniagent/search.py` | DeepSeek 官方 Search / Fetch 接口 |
| `miniagent/config.py` | 兼容已有配置，并统一装配 Agent、编程和搜索工具 |
| `launcher.py` | 双击启动、首次安装依赖和打开浏览器 |

每个会话使用一个 `Agent` 实例。默认每个任务最多调用模型 80 次，可选 800 次或不限制次数；一次模型回复可以包含多个工具调用，程序会把所有结果交回模型。`run()` 返回 `RunResult(status, answer, steps, tool_calls, error, diagnostic=None)`，状态为 `completed`、`step_limit`、`model_error`、`cancelled` 或 `stalled`。`completed` 表示模型给出最终回答，仍需按业务标准核验结果。

你可以直接通过聊天创建工具，也可以手写参数类、处理函数并注册，具体见 [扩展指南](docs/extension-guide.md)。文件读取工具限制在 `workspace/`；编程工具使用真实本机 Python 权限。默认宿主策略仅允许本次提供的编程工具及由它加载的工具对象，开发者另外加入的写工具仍需要自己的明确授权策略。

## 出错时如何反馈

如果更新后还出现相同报错，先看页面的服务版本。网页文件会在刷新时更新，Python 后台需要新进程才能加载代码；关闭原启动窗口后再双击 Start，或使用新版 Start 自动打开的新页面。配置、上传文件和已生成工具保存在原目录中，不需要重填。

展开失败消息下方的 **诊断信息**，点击 **复制诊断** 或 **下载 JSON**，把报告提供给维护者即可。报告中有版本号、运行编号、失败阶段与工具执行状态；流式字段错误会带具体字段路径、预期类型、实际类型和流片段编号。HTTP 状态码与上游请求编号仅在接口提供时显示。

报告自动保存为 `runs/<运行编号>.diagnostic.json`，对应完整运行日志为 `runs/<运行编号>.jsonl`。关闭服务后这些文件仍保留，原目录更新代码不会删除；磁盘写入失败时页面仍可复制或下载报告，并显示保存失败。

诊断报告只收集协议结构、类型、状态和代码位置，不收集请求头、原始响应正文、用户任务、思考正文或工具参数/结果；已配置的模型和搜索密钥会再次脱敏。完整 JSONL 日志含任务及工具数据，排查时优先分享诊断报告。

工具状态以宿主实际执行为准：**未执行** 表示参数仍在生成时运行就结束了，**已中断** 表示已发起执行但未收到结果，只有实际成功的工具结果才显示 **已完成**。模型流式参数中的可选 `null` 字段按“该字段本次没有更新”处理；错误数据类型、无效 JSON 和未完整结束的工具调用仍会报告错误，不会执行残缺参数。

## 开发者入口

如果已有 Python 开发环境，也可以从项目根目录启动网页：

```bash
python -m pip install -r requirements.txt
python -m miniagent.web
```

程序会自动打开浏览器。需要只启动服务时使用 `python -m miniagent.web --no-browser`；可以用 `--port 8765` 指定端口。

原有命令行接口仍可使用网页保存的配置：

```bash
python -m miniagent '读取 products.md，计算键盘和鼠标打九折后的总价'
python -m miniagent --chat
python -m examples.custom_tool '查询 REQ-001 的要求'
python -m unittest discover -s tests -v
```

自动化测试包含真实 Python 子进程、本机 HTTP 流式传输、提前送达、停止、SSE 重连、分片密钥脱敏、工具保存与重载、长循环，以及搜索接口契约。测试替身仅出现在测试代码中。交付未使用你的服务与凭据进行真实 API 联调；首次使用请核对配置、工具轨迹和最终结果。

学习对照：[CS329Z HW1](https://github.com/cs329z/assignment1-harness)、[Lecture 5：编排](https://web.stanford.edu/class/cs329z/slides/lecture05.pdf)、[Lecture 6：Agent 模式](https://web.stanford.edu/class/cs329z/slides/lecture06.pdf)。本项目为独立入门实现，没有复制课程作业代码。
