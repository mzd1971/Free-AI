"""Curated training datasets and local matching shared by the browser and Hub search."""
from difflib import SequenceMatcher
import re
from typing import NamedTuple
import unicodedata


class _Recommendation(NamedTuple):
    title: str
    repository: str
    description: str
    license: str
    topics: tuple[str, ...]


_CATALOG = (
    _Recommendation('中文问答与写作', 'llamafactory/alpaca_gpt4_zh',
                    '中文指令与回答，适合练习日常问答、写作和按要求完成任务。',
                    'apache-2.0', ('chinese', 'qa', 'writing')),
    _Recommendation('更多中文练习题', 'BelleGroup/train_0.5M_CN',
                    '覆盖问答、改写和翻译的中文资料；发布者限定仅供研究。',
                    'gpl-3.0', ('chinese', 'qa', 'writing')),
    _Recommendation('英文入门练习', 'yahma/alpaca-cleaned',
                    '整理过的英文指令与回答，适合熟悉训练流程。',
                    'cc-by-4.0', ('english', 'qa', 'writing')),
    _Recommendation('代码编写练习', 'sahil2801/CodeAlpaca-20k',
                    '英文编程要求与示例代码，适合练习函数编写和代码改写。',
                    'cc-by-4.0', ('english', 'code')),
    _Recommendation('数学推理解题', 'openai/gsm8k',
                    '英文数学应用题与分步解答；从训练部分取样，保留原测试题用于评测。',
                    'mit', ('english', 'math')),
    _Recommendation('人工编写对话', 'HuggingFaceH4/no_robots',
                    '人工编写的英文问答、写作与改写资料；仅限非商业用途。',
                    'cc-by-nc-4.0', ('english', 'humanchat', 'chat', 'qa', 'writing')),
)
RECOMMENDED_DATASETS = tuple((entry.title, entry.repository, entry.description) for entry in _CATALOG)
_RECOMMENDATIONS_BY_ID = {entry.repository.casefold(): entry for entry in _CATALOG}

_ALIASES = {
    'chinese': ('中文', '汉语', '简体中文', '简体', 'chinese', 'zh', 'cn'),
    'english': ('英文', '英语', 'english', 'en'),
    'qa': ('问答', '问题回答', 'qa', 'q&a', 'question answering', 'question answer'),
    'writing': ('写作', '改写', '文本创作', 'writing', 'creative writing', 'rewriting'),
    'code': ('代码', '编程', 'code', 'coding', 'programming'),
    'math': ('数学', '算术', '数学推理', '数学题', 'math', 'mathematics', 'arithmetic'),
    'humanchat': ('人工对话', '人工编写对话', '人工编写', 'human conversation', 'human chat'),
    'chat': ('对话', '聊天', '多轮对话', 'chat', 'conversation', 'dialogue'),
}
_TOPIC_BY_ALIAS = {alias: topic for topic, aliases in _ALIASES.items() for alias in aliases}
_CHINESE_ALIASES = re.compile('|'.join(
    sorted((alias for alias in _TOPIC_BY_ALIAS if not alias.isascii()), key=len, reverse=True)
))
_ENGLISH_PHRASES = re.compile(r'\b(?:' + '|'.join(
    re.escape(alias) for alias in _TOPIC_BY_ALIAS if alias.isascii() and (' ' in alias or '&' in alias)
) + r')\b')
_WORDS = re.compile(r'[^\W_]+')
_CAMEL_CASE = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')
_TOPIC_TAGS = {
    'chinese': {'language:zh', 'language:yue'},
    'english': {'language:en'},
    'qa': {'task_categories:question-answering', 'task_ids:extractive-qa', 'question-answering'},
    'writing': {'creative-writing', 'writing', 'rewriting'},
    'code': {'code', 'programming', 'task_ids:code-generation', 'modality:code'},
    'math': {'math', 'mathematics', 'math-word-problems'},
    'humanchat': {'human-chat', 'human-conversation', 'human-written-dialogue'},
    'chat': {'conversational', 'chat', 'dialogue', 'task_ids:dialogue-generation'},
}
_HUB_TOPICS = {
    'chinese': ('', 'language:zh'),
    'english': ('', 'language:en'),
    'qa': ('', 'task_categories:question-answering'),
    'writing': ('writing', ''),
    'code': ('code', ''),
    'math': ('math', ''),
    'humanchat': ('chat', 'language_creators:crowdsourced'),
    'chat': ('chat', ''),
}


def _words(value):
    return _WORDS.findall(unicodedata.normalize('NFKC', value).casefold())


def _repo_words(repository):
    return set(_words(repository)) | set(_words(_CAMEL_CASE.sub(' ', repository)))


_FUZZY_WORDS = set(_ALIASES).union(*(_repo_words(entry.repository) for entry in _CATALOG))


def _correct_word(word):
    if word in _FUZZY_WORDS or not word.isascii() or not 5 <= len(word) <= 32:
        return word
    nearby = sorted((
        (SequenceMatcher(None, word, candidate).ratio(), candidate)
        for candidate in _FUZZY_WORDS if len(candidate) >= 4 and abs(len(candidate) - len(word)) <= 2
    ), reverse=True)
    if nearby and nearby[0][0] >= 0.84 and (len(nearby) == 1 or nearby[0][0] - nearby[1][0] >= 0.03):
        return nearby[0][1]
    return word


def query_terms(query):
    """Normalize a query into terms shared by local and online result ranking."""
    query = unicodedata.normalize('NFKC', query).casefold().strip()
    query = _CHINESE_ALIASES.sub(lambda match: f' {_TOPIC_BY_ALIAS[match[0]]} ', query)
    query = re.sub(r'[-_]+', ' ', query)
    query = _ENGLISH_PHRASES.sub(lambda match: _TOPIC_BY_ALIAS[match[0]], query)
    return tuple(dict.fromkeys(
        _correct_word(_TOPIC_BY_ALIAS.get(word, word)) for word in _words(query)
    ))


def recommendation_details(repository):
    entry = _RECOMMENDATIONS_BY_ID.get(repository.casefold())
    if entry is None:
        return {}
    return {'title': entry.title, 'description': entry.description}


def _topic_matches(word, repo_words, tags, topics):
    if word in topics or tags & _TOPIC_TAGS[word]:
        return True
    if word == 'humanchat':
        return bool(tags & {'language_creators:crowdsourced', 'language_creators:expert-generated'}) and (
            bool(tags & _TOPIC_TAGS['chat']) or bool(repo_words & {'chat', 'conversation', 'dialogue'})
        )
    return any(alias in repo_words for alias in _ALIASES[word] if alias.isascii())


def dataset_match(query, terms, repository, tags=()):
    """Score complete name or topic matches; zero means unrelated."""
    if not terms:
        return 1

    entry = _RECOMMENDATIONS_BY_ID.get(repository.casefold())
    name = ''.join(_words(repository.rsplit('/', 1)[-1]))
    complete_name = ''.join(_words(repository))
    original = ''.join(_words(query))
    corrected = ''.join(terms)
    topic_only = all(term in _ALIASES for term in terms)
    topics = entry.topics if entry else ()
    tag_set = {tag.casefold() for tag in tags if isinstance(tag, str)}
    if topic_only:
        modalities = {tag for tag in tag_set if tag.startswith('modality:')}
        if modalities and not modalities & {'modality:text', 'modality:code'}:
            return 0
        if all(term in topics for term in terms):
            return 98

    if original == complete_name or (original == name and not topic_only):
        return 100
    if corrected in (name, complete_name):
        return 95
    if len(original) >= 3 and name.startswith(original) and not topic_only:
        return 90
    if not topic_only and len(corrected) >= 3 and name.startswith(corrected):
        return 85
    if len(terms) > 1 and len(corrected) >= 5 and corrected in name:
        return 75

    repo_words = _repo_words(repository)
    scores = []
    for word in terms:
        if word in repo_words:
            scores.append(30)
        elif word in _TOPIC_TAGS and _topic_matches(word, repo_words, tag_set, topics):
            scores.append(28 if word in topics else 22)
        elif word not in _TOPIC_TAGS and len(word) >= 3 and word in complete_name:
            scores.append(20 if any(token.startswith(word) for token in repo_words) else 14)
        elif entry and len(word) >= 2 and not word.isascii() and word in ''.join(_words(entry.title)):
            scores.append(20)
        else:
            return 0
    return 30 + sum(scores) / len(scores)


def recommend_datasets(query='', *, limit=20):
    """Return matching curated suggestions without making network requests."""
    terms = query_terms(query)
    if query.strip() and not terms:
        return []
    ranked = [
        (dataset_match(query, terms, entry.repository), entry)
        for entry in _CATALOG
    ]
    ranked.sort(key=lambda match: match[0], reverse=True)
    return [
        {'id': entry.repository, 'title': entry.title, 'description': entry.description,
         'license': entry.license, 'downloads': 0, 'likes': 0}
        for score, entry in ranked[:limit] if score > 0
    ]


def hub_search_parameters(query, terms):
    """Give the Hub up to three distinct queries, using filters for known purposes."""
    query = unicodedata.normalize('NFKC', query).strip()
    if not query:
        return [{'search': ''}]
    if not terms:
        return []

    def key(params):
        return (params.get('search', '').casefold(), tuple(params.get('filter', ())))

    requests = [{'search': query}]
    free_terms = [term for term in terms if term not in _HUB_TOPICS]
    hub_words, filters = [], []
    for term in terms:
        word, tag = _HUB_TOPICS.get(term, (term, ''))
        if word:
            hub_words.append(word)
        if tag:
            filters.append(tag)
    if hub_words or filters:
        expanded = {'search': ' '.join(hub_words)} if hub_words else {}
        if filters:
            expanded['filter'] = filters
        requests.append(expanded)

    suggested = recommend_datasets(query, limit=1)
    if suggested:
        requests.append({'search': suggested[0]['id']})
    if len(free_terms) == len(terms) > 1:
        requests.append({'search': ''.join(free_terms)})
    if len(free_terms) > 1:
        requests.append({'search': max(free_terms, key=len)})

    unique = {}
    for params in requests:
        unique.setdefault(key(params), params)
    return list(unique.values())[:3]
