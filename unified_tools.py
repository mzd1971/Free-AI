"""Local utility tools shared by assistant and project conversations."""

from pathlib import Path

from app_skills import execute_skill
import clock_context


_TOOLS = {
    'calculate': ('计算数字、括号及 + - * / // % 四则算式。', 'expression'),
    'check_json': ('检查 JSON 语法，整理缩进并定位格式错误。', 'text'),
    'current_time': ('读取设备当前日期、时间、星期和时区。', None),
    'summarize_csv': ('统计用户为本轮选定的 CSV，返回行数、空白和数值统计。', None),
}
UTILITY_NAMES = frozenset(_TOOLS)


def _schema(name):
    description, parameter = _TOOLS[name]
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'additionalProperties': False,
                       'properties': {parameter: {'type': 'string'}} if parameter else {},
                       'required': [parameter] if parameter else []},
    }}


def _skill_result(result, success_message=None):
    return {'ok': result['ok'], 'skill_id': result['skill_id'],
            'message': success_message if result['ok'] and success_message else result['answer'],
            'tool_execution_verified': result['tool_execution_verified'],
            'evidence': result['evidence'],
            **({'error': result['error']} if not result['ok'] else {})}


def calculate(expression):
    result = execute_skill('calculator', expression)
    return _skill_result(result, '已计算给定算式。')


def check_json(text):
    result = execute_skill('json_check', text)
    return _skill_result(result, 'JSON 格式检查通过。')


def device_time():
    now = clock_context.current_time()
    return {'ok': True, 'message': '已读取本机系统时钟。', 'tool_execution_verified': True,
            'evidence': {'source': 'system_clock', 'clock_at': now.isoformat(timespec='seconds'),
                         'date': now.date().isoformat(), 'time': now.strftime('%H:%M:%S'),
                         'weekday': '星期' + '一二三四五六日'[now.weekday()],
                         'timezone': now.tzname()}}


class UnifiedTools:
    def __init__(self, csv_path=None):
        self._csv_path = csv_path
        self.handlers = {'calculate': calculate, 'check_json': check_json, 'current_time': device_time}
        if csv_path is not None:
            self.handlers['summarize_csv'] = self.summarize_csv
        self.schemas = [_schema(name) for name in self.handlers]

    def summarize_csv(self):
        result = execute_skill('csv_summary', '', file_path=self._csv_path)
        if result['ok']:
            evidence = result['evidence']
            evidence['file_name'] = Path(evidence.pop('source')).name
            for column in evidence['columns']:
                column['name'] = column['name'].replace('\r', ' ').replace('\n', ' ')[:80]
        return _skill_result(result, '已统计用户选定的 CSV。')


def fit_skill_observation(observation, budget, encode):
    """Keep small verification fields and crop variable data to the context budget."""
    evidence = dict(observation['evidence'])
    observation = {**observation, 'evidence': evidence, 'truncated': True}
    # A shortened JSON value looks like malformed JSON, so omit its formatted copy.
    evidence.pop('formatted_json', None)
    columns = evidence.pop('columns', None)
    evidence.pop('warnings', None)
    if columns is not None:
        evidence['column_count'] = len(columns)
        low, high = 0, len(columns)
        while low < high:
            middle = (low + high + 1) // 2
            evidence['columns'] = columns[:middle]
            if len(encode(observation).encode('utf-8')) <= budget:
                low = middle
            else:
                high = middle - 1
        evidence['columns'] = columns[:low]
    return observation
