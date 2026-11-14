"""Read-only views of the bundled model directory; no downloads or model imports."""
from copy import deepcopy


def _entries(catalog):
    """Accept PackageManager.catalog, or the JSON document before manager loading."""
    if not isinstance(catalog, dict):
        raise TypeError('模型目录应为字典。')
    if isinstance(catalog.get('packages'), list):
        return ((spec['id'], spec) for spec in catalog['packages'])
    return catalog.items()


def chat_models(catalog):
    """Return independent model specs in catalog order, always including their IDs."""
    return [dict(deepcopy(spec), id=identity) for identity, spec in _entries(catalog)
            if spec.get('kind') == 'model']


def is_chat_model(catalog, identity):
    """An installed runtime, projector or training checkpoint is not a chat choice."""
    if not isinstance(identity, str) or not identity:
        return False
    return any(key == identity and spec.get('kind') == 'model'
               for key, spec in _entries(catalog))


def model_name(catalog, identity):
    """Resolve a short display name without silently choosing a fallback model."""
    if identity is None or identity == '':
        return '未选择模型'
    for key, spec in _entries(catalog):
        if key == identity and spec.get('kind') == 'model':
            return spec.get('short_name') or spec.get('title') or identity
    return str(identity)
