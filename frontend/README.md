# 对话界面开发

用户直接运行项目原有的 Python 启动器即可。`web/assets/chat.js` 和 `chat.css` 已经构建并提交，不需要 Node.js、npm 或在线 CDN。

会话界面使用 **assistant-ui**。`web/app.js` 保留本地应用的设置、文件、会话导航与 SSE 连接；`src/chat.tsx` 把服务器维护的消息转换为 assistant-ui 的 ExternalStoreRuntime。Python 服务仍然拥有任务状态、模型请求、工具执行和取消逻辑。前端显式关闭 `unstable_enableToolInvocations`，不会重复执行工具。

## 维护代码

开发者修改界面时，在此目录使用 Node.js 22 或更新版本：

```bash
npm ci --ignore-scripts
npm run typecheck
npm test
npm run build
```

构建会更新：

- `../web/assets/chat.js`：本地 React、assistant-ui 与 Markdown 组件。
- `../web/assets/chat.css`：组件样式，与现有桌面应用样式隔离。
- `../web/assets/THIRD_PARTY_NOTICES.txt`：实际进入构建的第三方依赖许可。
- `build-manifest.json`：依赖版本、产物大小与 SHA-256。

提交源码、锁文件和构建产物，勿提交 `node_modules`。固定依赖来自 `package-lock.json`，构建不会联网获取组件、字体或运行时资源。

界面版本由 `package.json` 在构建时写入 `FRONTEND_VERSION`，不在页面里手写第二份版本号。更新版本后须重新构建预编译资源。页面加载时将该版本与 `/api/bootstrap` 返回的后台运行版本核对；旧后台未提供 `runtime` 时，仅补查一次 `/api/health`。版本不一致或后台身份无效时，常驻提示要求重新双击 Start，并暂停新任务提交；草稿、历史、工具和设置仍可使用，已经运行的任务仍可停止。重新检查只由用户主动触发，不后台轮询。连接失败单独显示连接错误，不据此认定版本不一致。页脚显示实际后台版本，版本不一致时同时显示页面版本。

「复制运行诊断」只导出页面与后台版本、工作区和源码摘要、检查时间及页面来源地址，不导出聊天、配置、密钥、磁盘路径或 URL 查询参数。

## 组件来源与调整

`src/components/assistant-ui/` 采用 assistant-ui 官方开源元素，来源、固定提交和修改范围见 `UPSTREAM.json`；许可见 `ASSISTANT_UI_LICENSE.txt`。Thread 通过官方 Thread/Message primitives 组合，沿用官方 Reasoning、ToolFallback 和 MarkdownText 元素。项目只调整桌面布局、中文文案、已有输入框的接合与服务器消息格式。

- `ThreadPrimitive.Viewport` 负责流式自动滚动、用户滚动暂停和回到最新内容。
- `MessagePrimitive.GroupedParts` 管理稳定消息分段；官方 Reasoning 与 ToolFallback 管理展开状态、运行状态和可访问性。
- `MarkdownTextPrimitive` 与 `remark-gfm` 渲染表格、列表、引用、代码及其他 GFM 格式；React 默认转义 HTML，链接限制为 HTTP(S)，模型图片显示文字说明而不自动加载。
- `Remend` 仅修复正在流式到达的 Markdown 的显示副本。服务器文本、历史和复制的代码保持原文。关闭额外逐字动画，直接显示 SSE 已收到的内容。
- 官方 PrismLight 适配器和常用语言被直接打包；未识别语言保留可读代码。适配器源码来自官方包，避免模块入口同时拉入所有语言的高亮实现。
- 工具状态以 Python host 的执行阶段为准：生成参数后未执行、执行中断、实际工具失败与已完成分别显示。缺失的工具结果保持缺失，不会补成 `null`；工具真实返回 `null` 时仍可查看原值。
- 失败消息下的「诊断信息」使用同一套 Collapsible / Button 组件，展示失败字段、阶段、请求 ID、模型和端点，并复制或下载 host 已脱敏的诊断 JSON。报告跟随所属消息保留，后续运行不会覆盖之前的失败记录。前端不把聊天正文、模型原始响应或配置对象加入导出。

不添加前端模型 SDK、云会话服务或第二套工具执行循环。新增模型能力应继续通过 Python host 的 SSE 状态进入这个适配层。

## 验证范围

`npm test` 对实际生产消息适配器与停止请求边界运行回归：完整最终回复不会丢失 64 KiB 预览之后的文字，迟到的停止响应不会覆盖下一轮任务，错误提示不会重复输出已收到的正文；未执行或中断的工具不会被标成已完成，也不会生成假的 `null` 结果；诊断报告与失败消息绑定，继续对话后仍可查看。

版本检查回归覆盖同版放行、旧版阻断、缺失或无效后台身份、兼容旧健康检查元数据，以及运行诊断的字段白名单与地址脱敏。

修改后应验证真实 SSE 的提前输出、思考摘要、工具参数与结果、停止后保留部分内容、断线重连、跨会话隔离，以及 Markdown 表格、代码复制、高亮和不安全链接。浏览器测试脚本与截图保留在开发环境临时目录，不属于应用运行文件。
