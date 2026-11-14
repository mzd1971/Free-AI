"""Text-only default; import legacy training dependencies only when requested."""
_runtime = None
_cached = {}

def runtime():
    global _runtime
    if _runtime is None:
        from local_runtime import LocalRuntime
        _runtime = LocalRuntime()
    return _runtime

def shutdown():
    global _runtime
    if _runtime is not None:
        _runtime.shutdown()
        _runtime = None

def load_latest(agent_model=False):
    from legacy_qwen_chat import load_latest as legacy
    return legacy(agent_model)

def chat(message, history=None, use_base=False, agent_model=False, use_memory=True,
         memory_store=None, return_details=False, context=4096, image_path=None,
         on_token=None, cancel_event=None, legacy=False, model_id='text-default',
         skill_id='general', api_client=None, api_use_memory=False):
    if api_client is not None and image_path:
        raise ValueError('API 模式目前只支持文字，请移除图片后再发送。')
    if api_client is not None and legacy:
        raise ValueError('API 模式与本地训练模型模式不能同时使用。')
    if legacy:
        from legacy_qwen_chat import chat as legacy_chat
        return legacy_chat(message, history, use_base, agent_model, use_memory, memory_store, return_details)
    def generate(prompt):
        if skill_id != 'general':
            from app_skills import apply_skill
            prompt = apply_skill(skill_id, prompt)
        if api_client is not None:
            return api_client.generate(prompt, history=history, context=context,
                on_token=on_token, cancel_event=cancel_event)
        return runtime().generate(prompt, history, context=context,
            image_path=image_path, on_token=on_token, cancel_event=cancel_event,
            model_id=model_id)
    if api_client is not None:
        from epistemology import respond
        result = respond(message, generate, store=memory_store,
            use_memory=bool(api_use_memory), attempts=1, source='remote_api')
    elif use_memory:
        from epistemology import respond
        result = respond(message, generate, store=memory_store)
    else:
        result = {'answer': generate(message), 'episode_id': None, 'trace': [], 'verified': False}
    return result if return_details else result['answer']
