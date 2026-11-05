"""模型后端。接口只有一个: complete(system, history, user, meta) -> str (模型原始回复, 应为 JSON)。"""
from __future__ import annotations

import json
import os
from pathlib import Path


class OpenAIBackend:
    def __init__(self, model=None, max_tokens=4096):
        try:
            from openai import OpenAI
        except ImportError:
            raise SystemExit("真实后端需要: pip install openai, 并设置环境变量 OPENAI_API_KEY")
        self.client = OpenAI()
        self.model = model or os.environ.get("COMPANY_MODEL", "gpt-4.1")
        self.max_tokens = max_tokens

    def complete(self, system, history, user, meta):
        resp = self.client.responses.create(
            model=self.model, max_output_tokens=self.max_tokens, instructions=system,
            input=list(history) + [{"role": "user", "content": user}])
        return resp.output_text


SLUGIFY_BUGGY = '''import re


def slugify(text):
    # 故意留下的 bug: 忘了转小写
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")
'''
SLUGIFY_FIXED = '''import re


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''
SLUGIFY_TEST = '''import unittest

from slugify import slugify


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_spaces(self):
        self.assertEqual(slugify("  a   b  "), "a-b")


if __name__ == "__main__":
    unittest.main()
'''
TEST_CMD = 'python -m unittest discover -s deliverables/tech -p "test_*.py"'


class MockBackend:
    """离线演示: 每个角色按写死的脚本行事, 不调用任何模型, 用来演示流程与规则。

    演示里会发生三件事:
      1. 技术官第一次提交的代码有 bug -> 系统自己跑测试, 自动驳回 (不信自述)
      2. 创意官第一版只给了 2 个名字 -> 审核官对照规格驳回
      3. 技术官改了文件 -> 之前通过的 done 声明作废, 必须重新验收
    """

    def complete(self, system, history, user, meta):
        role, ws = meta["role"], Path(meta["workspace"])
        actions, say = [], ""
        for m in meta["messages"]:
            data = m.get("data") or {}
            if role == "chief":
                a, s = self._chief(m["event"], data, meta)
            elif role == "reviewer":
                a, s = self._reviewer(m["event"], data, ws)
            else:
                a, s = self._worker(role, m["event"], data)
            actions += a
            say = s or say
        return json.dumps({"say": say, "actions": actions}, ensure_ascii=False)

    def _chief(self, ev, data, meta):
        if ev == "goal":
            g = data["goal"]
            return ([
                {"type": "create_task", "title": "实现 slugify 工具函数", "owner": "tech",
                 "spec": "slugify(text): 转小写, 非字母数字的连续字符替换成单个连字符, 去掉首尾连字符; 附单元测试",
                 "required": ["done", "tested"]},
                {"type": "create_task", "title": f"调研简报: {g}", "owner": "intel",
                 "spec": "一页简报, 每条结论后面写明『来源』", "required": ["done"]},
                {"type": "create_task", "title": f"项目命名: {g}", "owner": "creative",
                 "spec": "给出 3 个候选名称, 每个一句话介绍 (列表项以 '- ' 开头)", "required": ["done"]},
            ], "拆成 3 个任务, 每个只有一个负责人。")
        if ev == "all_tasks_closed":
            rows = [f"- {t['id']} {t['title']} (负责人 {t['owner']}, {t['status']})"
                    for t in meta["board"]["tasks"].values()]
            return ([{"type": "final_report", "text": "# 最终报告\n\n全部任务已通过审核:\n\n" + "\n".join(rows) + "\n"}],
                    "全部关闭, 提交最终报告。")
        return [], ""

    def _worker(self, role, ev, data):
        if ev == "task_assigned":
            return self._work(role, data["task"], 1)
        if ev == "claim_rejected":
            return self._work(role, data["task"], 2)
        return [], ""

    def _work(self, role, task, attempt):
        tid = task["id"]
        if role == "tech":
            d = "deliverables/tech/"
            acts = [{"type": "write_file", "path": "slugify.py", "content": SLUGIFY_BUGGY if attempt == 1 else SLUGIFY_FIXED}]
            if attempt == 1:
                acts.append({"type": "write_file", "path": "test_slugify.py", "content": SLUGIFY_TEST})
            acts += [
                {"type": "claim", "task_id": tid, "kind": "done", "summary": "实现完成",
                 "evidence": [{"type": "file", "path": d + "slugify.py"}, {"type": "file", "path": d + "test_slugify.py"}]},
                {"type": "claim", "task_id": tid, "kind": "tested", "summary": "单元测试通过",
                 "evidence": [{"type": "command", "cmd": TEST_CMD}]},
            ]
            return acts, "第一版提交" if attempt == 1 else "已修复并重新提交"
        if role == "intel":
            body = ("# 调研简报 (演示占位内容, 非真实调研)\n\n"
                    "- 结论 1: 这里是占位结论。来源: (演示) 示例来源 A\n"
                    "- 结论 2: 这里是占位结论。来源: (演示) 示例来源 B\n")
            return [{"type": "write_file", "path": "brief.md", "content": body},
                    {"type": "claim", "task_id": tid, "kind": "done", "summary": "简报完成, 每条均有来源",
                     "evidence": [{"type": "file", "path": "deliverables/intel/brief.md"}]}], "简报已提交"
        if role == "creative":
            names = ["- 山海录: 一句话介绍 (演示)", "- 星轨: 一句话介绍 (演示)"]
            if attempt == 2:
                names.append("- 回声谷: 一句话介绍 (演示)")
            return [{"type": "write_file", "path": "names.md", "content": "# 候选名称\n\n" + "\n".join(names) + "\n"},
                    {"type": "claim", "task_id": tid, "kind": "done", "summary": "候选名称已给出",
                     "evidence": [{"type": "file", "path": "deliverables/creative/names.md"}]}], "已提交" if attempt == 1 else "已补齐第 3 个"
        return [], ""

    def _reviewer(self, ev, data, ws):
        if ev != "review_request":
            return [], ""
        t, c = data["task"], data["claim"]
        reason = None
        files = [e["path"] for e in c["evidence"] if e["type"] == "file"]
        text = "\n".join((ws / p).read_text(encoding="utf-8") for p in files)
        if t["owner"] == "creative":
            n = sum(1 for l in text.splitlines() if l.startswith("- "))
            if n < 3:
                reason = f"规格要求 3 个候选名称, 实际只有 {n} 个"
        if t["owner"] == "intel" and "来源" not in text:
            reason = "结论缺少来源"
        return ([{"type": "verdict", "task_id": t["id"], "claim_id": c["id"],
                  "decision": "reject" if reason else "accept",
                  "reason": reason or "系统已核验证据, 内容符合规格"}],
                "驳回" if reason else "通过")
