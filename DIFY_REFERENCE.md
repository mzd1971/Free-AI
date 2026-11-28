# Dify 参考范围

核对日期：2026-11-26。参考仓库：[langgenius/dify](https://github.com/langgenius/dify)，核对版本 `df053963e41fdce5f60acdece73d5d28d9bbf2fd`。

本轮优先完善 Free AI 的外置记忆，采用三项 Dify 中使用的产品结构：共享检索入口、临时检索测试参数、带来源的可查看结果。Free AI 继续使用本机 SQLite、原生界面和已引入的 Py-FSRS。

## 对照与实现

| Dify 中的做法 | Free AI 本轮实现 |
| --- | --- |
| 测试和正式使用共用检索 API | `preview_call` 和 `before_call` 共用 `_retrieve`，默认结果按上下文与 ID 逐字节验证 |
| 检索测试可临时改变 Top K / 下限 | “最多采用”“匹配要求”仅作用于当前预览，关闭抽屉后恢复默认 |
| 结构化返回内容和来源 | 展示原文、来源、范围、核实状态、匹配词及采用/未采用原因 |
| 会话历史按窗口和容量裁剪 | 保留 Free AI 现有聊天历史处理；跨会话长期记忆独立管理 |

Dify 的常规会话记忆以 `conversation.id` 为边界，不能直接等同于 Free AI 的跨会话长期记忆。Free AI 的关键词匹配分也不等同于 Dify 向量检索的相似度分数。

## 核对的源码和说明

- [Dify 检索测试说明](https://docs.dify.ai/en/cloud/use-dify/knowledge/test-retrieval)：测试与常规检索共用 API，测试设置临时生效。
- [HitTestingService](https://github.com/langgenius/dify/blob/df053963e41fdce5f60acdece73d5d28d9bbf2fd/api/services/hit_testing_service.py#L148-L160)：调用共享的检索服务。
- [检索结果结构](https://github.com/langgenius/dify/blob/df053963e41fdce5f60acdece73d5d28d9bbf2fd/api/core/workflow/nodes/knowledge_retrieval/retrieval.py#L23-L55)：内容、来源元数据及分数。
- [TokenBufferMemory](https://github.com/langgenius/dify/blob/df053963e41fdce5f60acdece73d5d28d9bbf2fd/api/core/memory/token_buffer_memory.py#L338-L348)：会话 ID 范围内的历史。
- [LLM 节点文档](https://docs.dify.ai/en/cloud/use-dify/nodes/llm#memory-and-file-processing)：节点级的同会话记忆。

## 直接复用的边界

Dify 的检索服务绑定其模型、数据库和服务层，当前 [API 依赖](https://github.com/langgenius/dify/blob/df053963e41fdce5f60acdece73d5d28d9bbf2fd/api/pyproject.toml) 与 Free AI 的轻量 Python 3.11 桌面运行时不同。因此本轮参考其接口和交互结构，不复制 Dify 前端或服务端源码，不随安装包启动整套服务。

Dify 的 [LICENSE](https://github.com/langgenius/dify/blob/df053963e41fdce5f60acdece73d5d28d9bbf2fd/LICENSE) 是带附加条件的 Apache 2.0 修改许可；后续如直接嵌入其代码，应按实际组件重新核对。本轮复用的第三方算法仍为已有 Py-FSRS，来源和许可保存在 `third_party/`。

工作流画布、文件知识库导入、向量检索和 Dify 服务连接不属于本轮已经完成的功能。
