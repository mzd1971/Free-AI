# 可选 Harness 项目执行组件

这是 Free AI 聊天中的可选执行器，默认内置执行器保持不变。外部执行、工具和会话恢复直接使用 DeepSeek Harness，Free AI 负责界面、模型连接、审批、记录和结束状态。

## 固定发行版与安装

Windows x64 采用 `deepseek-harness-runtime-bin==0.2.2rc2`，对应 CLI `0.2.2-rc.2`。下载 86,959,936 字节，校验 SHA-256 `a791af95a97be205a73354c6819e95b5fe2ff40a57ef8a78d53c188ee33e9aea`。安装器保留原 wheel 的许可证、资源清单和原生附属文件，不需要安装 Node、Python SDK 或 Pydantic，不修改全局 Python。

点击安装后才下载至应用数据目录暂存，先检查大小/哈希、解包范围和链接，再实际探测 ACP 新建与恢复。探测成功才发布组件目录，失败和取消不替换已有组件。官方项目及 wheel 遵循其 MIT 和随附第三方许可。

当前 Windows 发行版的原生加载对进程启动盘符敏感：从 D 盘目录启动可导致 `koffi.node` 找不到。适配器使用 `SystemRoot` 作为启动目录；执行组件、`DSH_HOME`、资源缓存、TEMP/TMP、项目目录仍在用户选择的非 C 盘位置。此策略已用实际官方 EXE 验证，未修补其二进制。

来源：[固定版运行时](https://pypi.org/project/deepseek-harness-runtime-bin/0.2.2rc2/)、[Python 运行时说明](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.2/python/sdk-runtime/README.md)。

## 协议与权限

标准库 Python 子进程通过 ACP JSON-RPC/NDJSON 通信。stdout 单行上限 4 MiB，队列和请求等待有界；取消先发送 `session/cancel`，协作取消后关闭会话以保存进度，不响应时回收进程树。不会因重试而自动换成新会话。

当前账户执行与 Free AI 现有命令语义相同。标准模式通过受信任 `tools/pre-execute` 插件对每个工具请求一次确认，保存上游拒绝/取消决定。ACP 插件依赖该审批插件提供的就绪服务，插件未加载则不能启动 ACP。自动模式仅在用户显式选中时使用。尚不能准确映射的只读/禁止写入组合在启动前拒绝，保留草稿。

原生沙箱的 `ask` 仅表示特定审批请求的处理方式，并不保证每条命令确认，因此不能把它直接当作 Free AI 标准模式。我们预先固定当前账户执行与逐操作审批，不在沙箱报错后自动放宽权限。子进程环境从内置执行器的白名单构造，仅额外注入选中模型的密钥和明确的 DSH 路径；无关服务凭据不传入。

来源：[ACP 接口](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.2/packages/acp/acp/README.md)、[工具执行扩展点](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.2/docs/tool-execution-pipeline.md)。

## 恢复与结果

每轮保存 Free AI 任务摘要、事件和独立的 Harness 会话编号。续跑验证工作区、模型和权限，传回同一会话编号；切换全局执行器不改变旧任务。会话最新记录在发送请求前原子保存，OS 文件锁防止同一会话并发执行。旧卡会解析到最新任务日记；旧版历史只能迁移可判定的单一线性链，分支或缺失会拒绝猜测。原始轮次文件不改写。本地模型连接在整轮保持独占锁，使用稳定的模型标识识别会话，模型服务换端口不会误判为换模型。

每次请求以 sessionId + runId 区分操作，不把下一轮结束当作上一轮中断的执行证据。最新任务卡提供人工核实入口；旧卡不能代替新卡核实。需求在 64 KiB UTF-8 范围内完整发送，完整脱敏文本另存 request.txt；task.json 仍保存有界摘要。超限先提示并保留草稿。

ACP `end_turn` 也可能来自受阻或错误，且此接口不提供完整可验收的测试退出码与计划。因此结束只显示“待验收”；明确失败、拒绝或仍在进行的工具操作显示“仍需处理”。聊天层现为两种执行器统一捕获有界项目文本快照，独立于内置文件工具的备份，支持差异预览和保留后改文件的撤回；不宣称已经同步 Free AI 长期记忆。

任务卡新增用户指定的命令验收，复用内置命令执行器，使用真实退出码、限长输出和运行前后项目文件指纹。只读模式禁止运行，取消和超时回收进程。不能确认文件版本时，即使退出码为零也不标为通过；单条检查通过不自动将整体任务完成。检查记录写入 SQLite 和对话供后续修复，重开或修改后标记需重跑。

## 验证范围

2026-11-19，Windows / Python 3.12.3：全量测试 **233 项，230 通过、3 跳过、0 失败、0 错误**。跳过当前账户无符号链接权限的测试，以及需要分发暂存目录的两项打包测试。原始日志在本机 `development/reports/harness-tests.txt`。

测试包括真实 ACP 子进程、Tk 界面和 SQLite 重开、安装器失败不发布、取消及连续两次恢复。另有实际官方 Harness EXE 实验：本机 OpenAI 兼容 SSE 端点按脚本生成工具调用，真实运行 Python 单元测试，先失败、修改文件后通过；拒绝操作不生成文件，停止后恢复同一会话。

```powershell
python -E -s -B -X utf8 -m unittest discover -s development/tests -p 'test_*.py' -v
python -E -s -B -X utf8 development/tests/run_harness_experiment.py --runtime 'D:\path\to\deepseek-harness-sdk-runtime-win-x64.exe'
```

这验证外部组件与 Free AI 的执行链路，**不是实际语言模型自主解决任务的能力测试**。模型工具模板、流式协议以及不同 API 服务仍需用户所选模型的实际验收。本轮未下载模型或创建新安装包。
