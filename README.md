# Minimal Agent

一个能在桌面浏览器里聊天、编程、创建可复用工具、搜索网页、上传文本文件并查看执行过程的 Python Agent。核心循环很短，方便阅读和扩展；界面与命令行共用同一个 Agent。

支持标准 **OpenAI Chat Completions** 和 **Anthropic Messages**，使用各自原生的 `tool_calls` / `tool_use`。应用调用你配置的真实 API；没有离线演示模式。

## 打开使用

1. 安装 **Python 3.11 或更新版本**。Windows 安装时勾选 **Add python.exe to PATH**。
2. 从 GitHub 同步项目到本机，或把整个压缩包解压到一个可以写入的文件夹；别直接在压缩包里打开文件。
3. Windows 双击 **Start.bat**；macOS 双击 **Start.command**。第一次会自动创建项目专用的 Python 环境并安装 Pydantic，需要联网，可能要等一小会儿。
4. 浏览器自动打开后，在模型设置中选择 **OpenAI** 或 **Anthropic**，填写 **模型名称**、**API Key** 并保存。接口地址可留空，使用官方默认地址。
5. 如需联网搜索，在同一设置窗口填写 **DeepSeek 搜索 API Key**。搜索使用独立的官方接口，主模型仍可以是 OpenAI 或 Anthropic。
6. 输入任务并发送。正常使用都在网页里完成，不需要输入命令。

启动窗口会保持打开，为网页提供本地服务；关闭它会停止服务。浏览器默认地址为 `http://127.0.0.1:8765`，端口被占用时会自动选择其他端口并打开正确地址。macOS 如果首次阻止打开脚本，可在 Finder 里右键文件，选择“打开”。

运行环境只有 Python 和 Pydantic；前端使用原生 HTML、CSS、JavaScript，无须安装 Node.js。

后续更新时，关闭启动窗口、同步 GitHub 的最新代码，再双击启动即可。`.gitignore` 会排除本机配置、虚拟环境、运行日志、新增的上传文件和 `local_tools/` 中生成的工具；仓库仅保留两份随项目提供的示例文本。

## 网页里能做什么

- **聊天与新建对话**：同一对话保留上下文，可以在侧栏切换会话。
- **上传文件**：支持 UTF-8 编码的 `.txt`、`.md`、`.json`、`.csv`，每个最多 2 MiB。文件保存在项目的 `workspace/`，同名文件自动改名。
- **查看执行过程**：查看模型调用阶段、工具名称、参数和返回结果。网页保留最近 100 条事件并限制单条预览长度，完整记录保存在本机 `runs/`，避免长任务拖慢界面。
- **编程**：`run_python` 在本机 Python 子进程中运行代码，可读取工作文件、生成结果、执行计算和处理文本。原来的 Calculator 已移除。
- **创建工具**：`create_tool` 运行样例测试，结果与预期相符才保存。下一次模型请求就能使用新工具；新对话和重启后也能使用。点击网页“工具”可查看已保存的代码和样例。
- **联网搜索**：`web_search` 搜索网页并返回真实来源链接；`fetch_webpage` 获取网页正文并支持分段读取。填入 DeepSeek 搜索 Key 后启用。
- **停止运行**：停止后不再发起下一次模型或工具调用；正在执行的 Python 子进程会被终止。模型请求的网络超时为 60 秒，搜索请求为 30 秒；这些正在进行的网络请求仍需等到返回或超时。
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

## 读懂并扩展

| 文件 | 职责 |
| --- | --- |
| `web/index.html`、`web/style.css`、`web/app.js` | 浏览器界面、样式与交互 |
| `miniagent/web.py` | 本地 HTTP 接口、会话、上传、后台运行和日志 |
| `miniagent/agent.py` | 模型调用 → 工具选择 → 验证和审批 → 执行 → 回传结果 |
| `miniagent/model.py` | 两家服务的原生工具调用适配器 |
| `miniagent/tools.py` | `ToolArgs` 严格参数验证和 `Tool` 执行边界 |
| `miniagent/builtin_tools.py` | `list_files`、`read_file` |
| `miniagent/programming.py`、`miniagent/python_worker.py` | Python 子进程、样例测试、工具保存与动态加载 |
| `miniagent/search.py` | DeepSeek 官方 Search / Fetch 接口 |
| `miniagent/config.py` | 兼容已有配置，并统一装配 Agent、编程和搜索工具 |
| `launcher.py` | 双击启动、首次安装依赖和打开浏览器 |

每个会话使用一个 `Agent` 实例。默认每个任务最多调用模型 80 次，可选 800 次或不限制次数；一次模型回复可以包含多个工具调用，程序会把所有结果交回模型。`run()` 返回 `RunResult(status, answer, steps, tool_calls, error)`，状态为 `completed`、`step_limit`、`model_error`、`cancelled` 或 `stalled`。`completed` 表示模型给出最终回答，仍需按业务标准核验结果。

你可以直接通过聊天创建工具，也可以手写参数类、处理函数并注册，具体见 [扩展指南](docs/extension-guide.md)。文件读取工具限制在 `workspace/`；编程工具使用真实本机 Python 权限。默认宿主策略仅允许本次提供的编程工具及由它加载的工具对象，开发者另外加入的写工具仍需要自己的明确授权策略。

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

自动化测试包含真实 Python 子进程、工具保存与重载、超时和停止、长循环、密钥保留与脱敏，以及搜索接口契约。测试替身仅出现在测试代码中。交付未使用你的服务与凭据进行真实 API 联调；首次使用请核对配置、工具轨迹和最终结果。

学习对照：[CS329Z HW1](https://github.com/cs329z/assignment1-harness)、[Lecture 5：编排](https://web.stanford.edu/class/cs329z/slides/lecture05.pdf)、[Lecture 6：Agent 模式](https://web.stanford.edu/class/cs329z/slides/lecture06.pdf)。本项目为独立入门实现，没有复制课程作业代码。
