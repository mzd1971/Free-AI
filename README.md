# Free AI

免费的 AI 客户端与独立训练端。当前版本：**2.4.2**，包含边缘行者主题、长期记忆和可在聊天中连续工作的项目助手。

![Free AI 边缘行者界面](assets/interface-preview.png)

## 运行

适用于 Windows x64。将仓库完整克隆到非 C 盘，例如 `D:\FreeAI`，然后打开：

- **Free AI.exe**：聊天、长期记忆、项目操作、按需下载模型或连接 API。
- **Free AI Trainer.exe**：准备教学资料、训练、比较结果和核实经验。

仓库保留两个启动程序和 Python 基础运行文件，不需要另外安装 Python。请保留整个目录结构；只下载单个 EXE 无法运行。启动不会自动下载或加载模型，也不附带任何人的聊天、长期记忆、API 配置或训练成果。

也可以使用带 Tcl/Tk 的 Python 3.11 从源码启动：

```powershell
python free_ai_bootstrap.py
python free_ai_bootstrap.py --trainer
```

界面和计算、JSON 检查、CSV 统计工具使用 Python 标准库。聊天模型、推理引擎、显卡加速和训练依赖按需安装；不要把系统 Python 环境与独立训练环境混用。

## 当前能力

- **本地模型或 API**：可下载文字模型，或配置兼容 Chat Completions 的服务。图片、语音和画图等扩展需要另外下载；API 路径目前只处理文字。
- **同一聊天中做项目**：选择项目目录，让助手查看文件、编辑代码、运行测试，并根据结果继续处理。命令在聊天中逐条确认；可停止任务或撤回文件工具的修改。
- **长期记忆**：手工定义偏好、事实和做法；AI 可直接记下并通知，标为尚未核实。手工记录优先，项目记忆按目录隔离，支持编辑、停用和删除。联网模型读写长期记忆需要单独开启。
- **经验与训练**：长期记忆不等于参数训练；任务经验可以在训练端核实，真正训练使用独立的模型与数据流程。小模型的回复和代码仍需通过实际检查。
- **界面皮肤**：黑底、青色电路、切角面板和荧光绿按钮。在“设置 → 界面皮肤”可切换边缘行者与经典深色，重新打开生效。训练端使用浅色界面。

详细操作见 [使用说明](使用说明.md) 和 [安装说明](安装说明.txt)。

## 文件位置

应用默认位于 `D:\FreeAI`。新生成的聊天、长期记忆和训练记录进入 `portable_data`，模型默认进入 `models`；模型位置可以在安装时另选。个人数据、下载文件、密钥配置、机器路径和构建临时文件均由 `.gitignore` 排除。

仓库历史版本曾包含本机数据库和生成记录。当前版本停止跟踪这些文件，但既有 Git 历史没有被重写。

## 检查与构建

```powershell
& '.\Free AI.exe' --self-test
& '.\Free AI Trainer.exe' --self-test
& '.\runtime\python\python.exe' -E -s -B -X utf8 -m unittest discover -s development/tests -p 'test_*.py' -v
```

自检使用隔离数据；真实模型实验在 `development/tests/run_*_experiment.py`，需先在应用里下载相应模型。打包测试依赖准备后的分发目录，未准备时相关项会跳过。

准备分发文件：

```powershell
& '.\development\build_lite.ps1' -PrepareOnly
```

此步骤使用随附 Python 和 Windows .NET C# 编译器生成两个 EXE，分发目录不含模型或个人数据。要生成安装 EXE，还需要自行准备可用的 Inno Setup 编译器：

```powershell
& '.\development\build_lite.ps1' -Compiler 'D:\Tools\Inno Setup\ISCC.exe' -ReleaseDirectory 'D:\FreeAI-Releases'
```

当前本机已验证客户端、训练端、长期记忆和项目流程；**尚未生成本版安装包**。仓库中的两个应用 EXE 是启动入口，不是安装程序。

图标原图和多尺寸资源位于 `assets`，可通过 `development/export_icon.ps1` 重新导出。Python 及随附运行库保留其许可证；按需下载的模型和引擎遵循各自许可，来源、版本与校验信息见 `capability_catalog.json`。
