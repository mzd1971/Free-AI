"""Use the installed llama.cpp service for project tool conversations."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from remote_api import APICancelled, APIClient, APIError


class _LocalToolClient(APIClient):
    def _request(self, endpoint, state, data=None):
        if data is not None and endpoint == '/chat/completions':
            data = dict(data, temperature=.1, parallel_tool_calls=False)
        return super()._request(endpoint, state, data)


def _loopback_api_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == 'http' and ipaddress.ip_address(parsed.hostname).is_loopback
                and parsed.port and parsed.username is None and parsed.password is None
                and parsed.path in ('', '/') and not parsed.query and not parsed.fragment):
            return f'http://{parsed.netloc}/v1'
    except (TypeError, ValueError):
        pass
    raise APIError('本地模型返回了无效的本机服务地址。')


class LocalProjectModel:
    """Project-model adapter. An explicitly supplied runtime remains caller-owned."""

    def __init__(self, runtime=None, model_id='text-default', max_output_tokens=2048):
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError('请选择本地项目模型。')
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 32768:
            raise ValueError('回答长度上限必须是 1 到 32768 的整数。')
        self._runtime = runtime
        self.model_id = model_id
        self.max_output_tokens = max_output_tokens

    def complete_tools(self, messages, tools, cancel_event=None, context=16384):
        from local_runtime import MissingCapability, RuntimeCancelled

        cancelled_message = '已停止本地项目模型的本次生成。'
        if cancel_event is not None and cancel_event.is_set():
            raise APICancelled(cancelled_message)
        runtime = self._runtime
        if runtime is None:
            from qwen_chat import runtime as shared_runtime

            try:
                runtime = shared_runtime()
            except Exception:
                raise APIError('无法初始化本地运行组件，请检查能力中心和本机配置。') from None
        if not runtime._request_lock.acquire(blocking=False):
            raise APIError('另一个本地任务正在进行，请等它完成或先停止。')

        cancelled = False
        try:
            try:
                url = runtime.ensure_server(context, False, cancel_event, self.model_id)
            except (RuntimeCancelled, APICancelled):
                raise
            except MissingCapability as error:
                if error.package_id in ('engine-cpu', 'engine-cuda'):
                    message = '本地运行组件缺失或不完整，请在能力中心安装或修复。'
                else:
                    message = '所选本地模型缺失或不完整，请在能力中心下载或修复。'
                raise APIError(message) from None
            except ValueError:
                raise APIError('所选本地模型或上下文档位不可用。请检查模型库和上下文设置。') from None
            except Exception:
                raise APIError('无法启动本地模型。请检查能力中心、可用内存和本地运行记录。') from None
            try:
                client = _LocalToolClient(_loopback_api_url(url), runtime._alias(self.model_id),
                                          runtime._token, max_output_tokens=self.max_output_tokens)
            except (AttributeError, TypeError, ValueError):
                raise APIError('本地工具调用连接配置无效。') from None
            client.request_timeout = max(client.request_timeout, getattr(runtime, 'request_timeout', 300), 300)
            return client.complete_tools(messages, tools, cancel_event=cancel_event, context=context)
        except (RuntimeCancelled, APICancelled):
            cancelled = True
            raise APICancelled(cancelled_message) from None
        finally:
            try:
                if self._runtime is None and (cancelled or cancel_event is not None and cancel_event.is_set()):
                    try:
                        runtime.shutdown()
                    except Exception:
                        pass
            finally:
                runtime._request_lock.release()
