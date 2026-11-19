# Windows 仓库启动修复

2026-11-19，基线 main `a0c0ba3`。在用户实际工作区运行 Free AI.exe 与 Free AI Trainer.exe 均退出 1。错误分别发生于导入 `_ctypes` 和 `_tkinter`；同一随附 Python 的单行 import 可稳定复现。

根因是 `.gitignore` 中的 `*.py[cod]` 同时忽略了 `.pyc`、`.pyo` 和 Windows 原生扩展 `.pyd`。原仓库跟踪了 Python 和依赖 DLL，却没有跟踪 DLLs 目录中的扩展模块。系统 Python 上的业务回归不能覆盖这个分发缺陷；此前的两项分发测试又因未准备 staging 跳过。

修复将缓存规则明确拆为 `.pyc` 和 `.pyo`，补回同一 Python 3.11.15 构建的 21 个非测试扩展，总计 2,743,296 字节。来源为本机保留的 uv 管理运行时；复制前核对 python.exe、python311.dll、python3.dll、libffi、libssl、Tcl、Tk 七个核心文件 SHA-256，均与原仓库一致；复制后再次核对扩展哈希。没有混用 Python 3.12 动态库，没有升级解释器或下载模型。

新增 `development/tests/test_checkout_runtime.py`，直接启动仓库里的两个 EXE，并在清理 Python/Tcl 搜索环境、只保留系统级 PATH 的条件下检查随附 Python、SSL、SQLite、ctypes 及真实 Tk 窗口初始化。测试验证实际加载路径位于应用 runtime 内；独立于分发暂存目录，仅非 Windows 平台跳过。启动自检使用隔离目录，并保留原错误日志。

验证结果：

- 修复前新增 3 项启动测试全部失败；修复后 3 项全部通过。
- `build_lite.ps1 -PrepareOnly` 成功，暂存包约 45.7 MB，不含模型或个人数据。
- 在实际工作区使用 `runtime/python/python.exe` 跑全套：268 项，267 通过，1 跳过，0 失败，耗时约 90 秒。唯一跳过是当前 Windows 账户无创建符号链接权限；两项分发检查已实际通过。
- 正常启动当前工作区 Free AI.exe，确认子进程出现可见的 Free AI 主窗口。

本机日志：`development/reports/checkout-runtime-before.txt`、`checkout-runtime-after.txt`、`bundled-runtime-tests-20261119-231402.txt`。本轮未生成安装程序。
