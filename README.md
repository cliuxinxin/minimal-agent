# Minimal Agent

一个能在桌面浏览器里直接聊天、上传文本文件、查看工具执行过程的 Python Agent。核心循环很短，方便阅读和扩展；界面与命令行共用同一个 Agent。

支持标准 **OpenAI Chat Completions** 和 **Anthropic Messages**，使用各自原生的 `tool_calls` / `tool_use`。应用调用你配置的真实 API；没有离线演示模式。

## 打开使用

1. 安装 **Python 3.11 或更新版本**。Windows 安装时勾选 **Add python.exe to PATH**。
2. 从 GitHub 同步项目到本机，或把整个压缩包解压到一个可以写入的文件夹；别直接在压缩包里打开文件。
3. Windows 双击 **Start.bat**；macOS 双击 **Start.command**。第一次会自动创建项目专用的 Python 环境并安装 Pydantic，需要联网，可能要等一小会儿。
4. 浏览器自动打开后，在模型设置中选择 **OpenAI** 或 **Anthropic**，填写 **模型名称**、**API Key** 并保存。接口地址可留空，使用官方默认地址。
5. 输入任务并发送。正常使用都在网页里完成，不需要输入命令。

启动窗口会保持打开，为网页提供本地服务；关闭它会停止服务。浏览器默认地址为 `http://127.0.0.1:8765`，端口被占用时会自动选择其他端口并打开正确地址。macOS 如果首次阻止打开脚本，可在 Finder 里右键文件，选择“打开”。

运行环境只有 Python 和 Pydantic；前端使用原生 HTML、CSS、JavaScript，无须安装 Node.js。

后续更新时，关闭启动窗口、同步 GitHub 的最新代码，再双击启动即可。`.gitignore` 会排除本机配置、虚拟环境、运行日志和新增的上传文件；仓库仅保留两份随项目提供的示例文本。

## 网页里能做什么

- **聊天与新建对话**：同一对话保留上下文，可以在侧栏切换会话。
- **上传文件**：支持 UTF-8 编码的 `.txt`、`.md`、`.json`、`.csv`，每个最多 2 MiB。文件保存在项目的 `workspace/`，同名文件自动改名。
- **查看执行过程**：查看模型调用阶段、工具名称、参数和返回结果。
- **停止运行**：停止后不再发起下一次模型或工具调用。已经开始的网络请求或同步工具会等到返回或超时；界面在此期间显示“正在停止”。模型请求超时为 60 秒。
- **模型设置**：只保留服务商、模型、API Key、可选接口地址四项。保存设置后新建对话使用新模型；已有对话保留创建时的配置。

可以先发送：

> 读取 products.md，计算键盘和鼠标打九折后的总价。

项目提供了这份文本作为真实文件工具的输入。模型会决定是否读取和计算，结果通过实际 API 返回。上传自己的文件后，在消息中直接引用文件名即可。

## 配置和会话

网页设置保存在项目的 `config.json`，无需手工编辑。配置仅接受 `provider`、`model`、`api_key` 和可选 `base_url`。模型名称应填写该服务实际支持的名称。

| 服务商 | 标准接口 | 默认基础地址 |
| --- | --- | --- |
| `openai` | Chat Completions | `https://api.openai.com/v1` |
| `anthropic` | Messages | `https://api.anthropic.com` |

API Key 不写入浏览器存储，读取配置时服务端只返回“是否已设置”；它以明文保存在本机 `config.json`，请勿把这个文件提交或分享。保持服务商和接口地址不变时，编辑设置可把密钥留空以沿用现有值；更改服务商或接口地址时需要重新填写密钥。

对话保存在本地服务的内存中：刷新网页会保留，关闭服务会清空。配置、上传文件和 `runs/*.jsonl` 执行日志保存在磁盘中；日志用于检查运行过程，不会自动恢复会话。日志可能含任务、文件和工具结果，请按原始业务数据管理。

## 读懂并扩展

| 文件 | 职责 |
| --- | --- |
| `web/index.html`、`web/style.css`、`web/app.js` | 浏览器界面、样式与交互 |
| `miniagent/web.py` | 本地 HTTP 接口、会话、上传、后台运行和日志 |
| `miniagent/agent.py` | 模型调用 → 工具选择 → 验证和审批 → 执行 → 回传结果 |
| `miniagent/model.py` | 两家服务的原生工具调用适配器 |
| `miniagent/tools.py` | `ToolArgs` 严格参数验证和 `Tool` 执行边界 |
| `miniagent/builtin_tools.py` | `list_files`、`read_file`、`calculate` |
| `miniagent/config.py` | 读取四项配置并装配 Agent |
| `launcher.py` | 双击启动、首次安装依赖和打开浏览器 |

每个会话使用一个 `Agent` 实例。默认每个任务最多调用模型 8 次；一次模型回复可以包含多个工具调用，程序会把所有结果交回模型。`run()` 返回 `RunResult(status, answer, steps, tool_calls, error)`，状态为 `completed`、`step_limit`、`model_error` 或 `cancelled`。`completed` 表示模型给出最终回答，仍需按业务标准核验结果。

新增工具通常只需要参数类、处理函数和一行注册，具体见 [扩展指南](docs/extension-guide.md)。内置工具均为只读；文件工具限制在 `workspace/`，路径限制不等于操作系统沙箱。网页没有写操作审批面板，有副作用的工具默认不执行，需要扩展者设计明确的确认流程。

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

测试替身仅出现在测试代码中。交付未使用你的服务与凭据进行真实 API 联调；首次使用请核对配置、工具轨迹和最终结果。

学习对照：[CS329Z HW1](https://github.com/cs329z/assignment1-harness)、[Lecture 5：编排](https://web.stanford.edu/class/cs329z/slides/lecture05.pdf)、[Lecture 6：Agent 模式](https://web.stanford.edu/class/cs329z/slides/lecture06.pdf)。本项目为独立入门实现，没有复制课程作业代码。
