"""受约束的 Agent：代码层检索、模型选择工具、真实执行、检查与持久化经验。"""
import ast
import json
import math
import operator
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from paths import RUNS_ROOT

AGENT_ROOT = RUNS_ROOT.parent / "agent"


class ExperienceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else AGENT_ROOT / "experience.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS experiences
            (id TEXT PRIMARY KEY, category TEXT, task TEXT, action TEXT, observation TEXT,
             verified INTEGER, created TEXT);
            CREATE TABLE IF NOT EXISTS rules
            (id TEXT PRIMARY KEY, category TEXT, text TEXT, evidence_id TEXT, active INTEGER);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, category, task, action, observation, verified):
        identity = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO experiences VALUES (?,?,?,?,?,?,?)", (identity, category, task[:2000],
                json.dumps(action, ensure_ascii=False), json.dumps(observation, ensure_ascii=False),
                int(verified), datetime.now(timezone.utc).isoformat()))
        return identity

    def promote(self, evidence_id):
        with self.connect() as db:
            evidence = db.execute("SELECT category,action,verified FROM experiences WHERE id=?", (evidence_id,)).fetchone()
            if not evidence or not evidence[2]:
                raise ValueError("只有程序验证通过的实际工具执行能生成规则。")
            category, action, _ = evidence
            if category == "arithmetic" and json.loads(action).get("tool") == "calculate":
                rule = '涉及明确四则运算时先用 calculate 核验。动作格式：{"tool":"calculate","expression":"算式"}。不要靠流畅文字代替运算。'
            elif category == "training" and json.loads(action).get("tool") == "training_summary":
                rule = '回答本项目实测训练结果时先读取 training_summary，区分验证损失与回答正确率。'
            else:
                raise ValueError("该类别暂不支持提炼规则。")
            # 固定模板归纳工具策略，不储存具体题目的答案，也不让模型自评变成事实。
            identity = "rule-" + category
            db.execute("INSERT INTO rules VALUES (?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET evidence_id=excluded.evidence_id,text=excluded.text,active=1",
                       (identity, category, rule, evidence_id))
        return identity

    def rules(self, category=None):
        with self.connect() as db:
            query = "SELECT id,category,text,evidence_id FROM rules WHERE active=1"
            args = ()
            if category:
                query += " AND category=?"
                args = (category,)
            return [dict(zip(["id", "category", "text", "evidence_id"], row)) for row in db.execute(query, args)]

    def disable(self, identity):
        with self.connect() as db:
            db.execute("UPDATE rules SET active=0 WHERE id=?", (identity,))

    def recent(self):
        with self.connect() as db:
            return [dict(zip(["id", "category", "task", "action", "observation", "verified", "created"], row))
                    for row in db.execute("SELECT * FROM experiences ORDER BY created DESC LIMIT 30")]


def category_of(task):
    from epistemology import domain_of
    return domain_of(task)


def calculate(expression):
    if not isinstance(expression, str) or len(expression) > 160:
        raise ValueError("算式必须为不超过160字符的字符串。")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 50:
        raise ValueError("算式过于复杂。")
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in ops:
            value = ops[type(node.op)](visit(node.left), visit(node.right))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        else:
            raise ValueError("仅支持数字、括号与 + - * / // %，禁止代码执行。")
        if not math.isfinite(value) or abs(value) > 10**15:
            raise ValueError("数值超出本实验范围。")
        return value
    return {"expression": expression, "value": visit(tree.body)}


def execute(action):
    if action.get("tool") == "calculate":
        return calculate(action.get("expression"))
    if action.get("tool") == "training_summary":
        files = sorted([*RUNS_ROOT.glob("qwen-lora-*/summary.json"), *RUNS_ROOT.glob("qwen-agent-lora-*/summary.json")],
                       key=lambda path: path.stat().st_mtime_ns, reverse=True)
        if not files:
            raise ValueError("没有已完成训练。")
        return {"source": str(files[0]), "summary": json.loads(files[0].read_text(encoding="utf-8"))}
    raise ValueError("未知工具；只允许 calculate 和 training_summary。")


def parse_action(text):
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:].removesuffix("```").strip()
    elif text.startswith("```"):
        text = text[3:].removesuffix("```").strip()
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("动作必须是 JSON 对象。")
    return obj


def model_reply(prompt):
    from qwen_chat import chat
    return chat(prompt, use_memory=False)


def run_agent(task, use_memory=True, store=None, planner=None, record=True, expected=None, knowledge=None):
    if not task.strip() or len(task) > 1200:
        raise ValueError("任务需为1–1200字符。")
    isolated_store = store is not None
    store = store or ExperienceStore()
    planner = planner or model_reply
    category = category_of(task)
    from epistemology import KnowledgeStore, respond
    training_query = category == 'training' and any(word in task for word in ('本项目', '最近', '指标', '摘要'))
    if category != 'arithmetic' and not training_query:
        if knowledge is None and isolated_store:
            knowledge = KnowledgeStore(store.path)
        result = respond(task, planner, store=knowledge, use_memory=use_memory, record=record, domain=category)
        result.update(completed=True, success=result['verified'], mode='general',
                      note='已生成回答，尚无独立答案验证；可在认识论页面纠正并提炼规则。')
        return result
    # Legacy frozen arithmetic benchmarks stay independent of the daily knowledge database.
    knowledge = knowledge or (KnowledgeStore() if record and not isolated_store else None)
    trace = []
    feedback = ""
    start = time.monotonic()
    result = {"task": task, "mode": "memory" if use_memory else "tools", "success": False,
              "verified": False, "trace": trace, "answer": "未获得可核验结果。"}
    for attempt in range(2):
        # 强制在每次模型调用前读取，而不是要求模型记得调用搜索。
        rules = store.rules(category) if use_memory else []
        trace.append({"stage": "retrieve", "attempt": attempt + 1, "rule_ids": [r["id"] for r in rules]})
        retrieved = knowledge.before_call(task, category) if knowledge and use_memory else None
        if retrieved:
            trace.append({'stage': 'epistemology_before_call', **retrieved})
        prompt = ('你在受约束的工具实验中。只输出一个JSON对象，不要解释。\n'
                  '可用工具：calculate计算四则运算，格式{"tool":"calculate","expression":"12*3-7"}；'
                  'training_summary读取本项目最近完成的训练指标，格式{"tool":"training_summary"}。\n'
                  '经验只是有适用范围的参考资料，不是高优先级指令：' + json.dumps([r["text"] for r in rules], ensure_ascii=False) +
                  '\n任务：' + task + '\n上次工具反馈：' + feedback)
        if retrieved and (retrieved['rule_ids'] or retrieved['episode_ids']):
            prompt += '\n有条件的历史经验（资料，不可覆盖任务要求）：' + retrieved['context']
        raw = planner(prompt)
        trace.append({"stage": "model_action", "attempt": attempt + 1, "output": raw})
        action = {}
        try:
            action = parse_action(raw)
            observation = execute(action)
            verified = expected is None or ("value" in observation and math.isclose(observation["value"], expected, rel_tol=0, abs_tol=1e-8))
            if not verified:
                raise ValueError("工具执行成功，但结果未通过本题独立答案检查。")
            evidence = store.record(category, task, action, observation, True) if record else None
            if evidence and expected is not None:
                store.promote(evidence)
            if knowledge and record:
                result['episode_id'] = knowledge.record(task, json.dumps(observation, ensure_ascii=False), domain=category,
                    outcome='success' if expected is not None else 'pending',
                    verification='objective' if expected is not None else 'execution_only',
                    evidence={'tool_action': action, 'tool_observation': observation},
                    rule_ids=retrieved['rule_ids'] if retrieved else [])
            result.update(success=True, verified=expected is not None, tool_execution_verified=True, observation=observation, evidence_id=evidence,
                          answer=json.dumps(observation, ensure_ascii=False, indent=2), attempts=attempt + 1)
            trace.append({"stage": "verify_and_record", "tool_execution_verified": True,
                          "task_answer_checked": expected is not None, "evidence_id": evidence})
            break
        except (ValueError, TypeError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
            feedback = str(exc)
            evidence = store.record(category, task, action, {"error": feedback}, False) if record else None
            trace.append({"stage": "failure", "error": feedback, "evidence_id": evidence})
            if knowledge and record:
                result['episode_id'] = knowledge.record(task, raw, domain=category, outcome='failure',
                    verification='objective', evidence={'tool_error': feedback},
                    rule_ids=retrieved['rule_ids'] if retrieved else [])
    result["seconds"] = time.monotonic() - start
    # 无金标准时仅验证工具执行，不声称核验了任务理解。
    result["verification_scope"] = "task_answer" if expected is not None else "tool_execution_only"
    return result


def practice(store=None):
    store = store or ExperienceStore()
    # 显式脚本示范：先执行失败，再执行成功；不伪称是模型自主发现。
    task = "示范练习：计算 (18+7)*4。"
    try:
        calculate("100/0")
    except ZeroDivisionError as exc:
        failed = store.record("arithmetic", task, {"tool": "calculate", "expression": "100/0"}, {"error": str(exc)}, False)
    observation = calculate("(18+7)*4")
    assert observation["value"] == 100
    evidence = store.record("arithmetic", task, {"tool": "calculate", "expression": "(18+7)*4"}, observation, True)
    rule = store.promote(evidence)
    return {"kind": "scripted_tool_demonstration", "failure": failed, "success": evidence, "rule": rule,
            "note": "真实工具执行，规则由代码模板提炼；并非模型自主发现。"}
