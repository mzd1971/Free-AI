"""Fresh device-clock facts and narrowly scoped local date/time answers."""
import datetime
import re
import unicodedata


_PREFIX = r'(?:(?:请|请问|麻烦|帮我|帮忙|告诉我|(?:联网)?(?:搜索|搜|查询|查)(?:一下|一查|查)?|看一下|看看))*'
_DATE = re.compile(_PREFIX + r'(?:今天|今日|现在|当前)(?:的)?(?:是)?(?:几号|几月几号|几月几日|日期|什么日期|多少号|哪一天|几月|哪年|哪一年)(?:是多少|是什么|是几号)?(?:呀|啊|呢|吗)?\Z')
_WEEKDAY = re.compile(_PREFIX + r'(?:今天|今日|现在)(?:是)?(?:星期几|周几|礼拜几)(?:呀|啊|呢|吗)?\Z')
_TIME = re.compile(_PREFIX + r'(?:现在|当前|目前)(?:的)?(?:是)?(?:几点|几点钟|时间)(?:了|是多少|是什么)?(?:呀|啊|呢|吗)?\Z')
_ENGLISH = re.compile(r"(?:(?:please|tellme|check|search)\s*)?(?:what(?:'s|is)|whatis)?(?:the)?(date|time|dayoftheweek)(?:isittoday|isitnow|isit|today|now)?\Z")


def current_time():
    return datetime.datetime.now().astimezone()


def clock_query_kind(message):
    if not isinstance(message, str) or len(message) > 100:
        return None
    text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', message).casefold()).replace('’', "'").strip('?!。！？，,.')
    for kind, pattern in (('date', _DATE), ('weekday', _WEEKDAY), ('time', _TIME)):
        if pattern.fullmatch(text):
            return kind
    english = _ENGLISH.fullmatch(text)
    if english:
        return {'dayoftheweek': 'weekday'}.get(english[1], english[1])
    if re.fullmatch(r"(?:please|tellme|search)?(?:what(?:'s|is))?(?:today(?:'s)?|thecurrent)date", text):
        return 'date'
    if re.fullmatch(r'whatday(?:isittoday|isit)', text):
        return 'weekday'
    return None


def _values(now):
    offset = now.strftime('%z')
    zone = 'UTC' + offset[:3] + ':' + offset[3:] if offset else '本机时区'
    date = f'{now.year}年{now.month}月{now.day}日'
    weekday = '星期' + '一二三四五六日'[now.weekday()]
    return date, weekday, zone


def temporal_context(*, compact=False):
    now = current_time()
    if compact:
        return f'本轮本机时间：{now.isoformat(timespec="seconds")}。旧记忆不能替代当前时钟。'
    date, weekday, zone = _values(now)
    return (f'本轮设备时钟：{date}，{weekday}，{now:%H:%M:%S}（{zone}）。'
            '这是电脑本地时间；今天、现在以此为准，长期记忆和历史回答中的旧日期不能替代它。'
            '未实际取得网页结果时，不得声称已联网搜索。')


def local_clock_reply(message):
    kind = clock_query_kind(message)
    if kind is None:
        return None
    now = current_time()
    date, weekday, zone = _values(now)
    text = f'现在是 {now:%H:%M:%S}，今天是 {date}，{weekday}。' if kind == 'time' else f'今天是 {date}，{weekday}。'
    answer = text + f'\n\n来源：本机系统时钟（{zone}）。'
    return {'answer': answer, 'episode_id': None, 'trace': [], 'verified': True,
            'source': 'system_clock', 'clock_at': now.isoformat(),
            'memory_attempted': False, 'memory_status': 'not_attempted',
            'verification_note': '已读取本机系统时钟。'}
