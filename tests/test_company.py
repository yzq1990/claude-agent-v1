import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from company.board import Board, BoardError  # noqa: E402
from company.llm import MockBackend  # noqa: E402
from company.office import Office  # noqa: E402

CMD = "python -m unittest"


class BoardRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.b = Board(self.ws, [CMD])
        d = self.ws / "deliverables" / "tech"
        d.mkdir(parents=True)
        (d / "a.txt").write_text("hello", encoding="utf-8")
        self.f = d / "a.txt"
        self.ev = [{"type": "file", "path": "deliverables/tech/a.txt"}]

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, required):
        return self.b.create_task("t", "tech", "spec", required, ["tech"])

    def test_single_owner_only(self):
        with self.assertRaises(BoardError):
            self.b.create_task("t", ["tech", "intel"], "", ["done"], ["tech", "intel"])
        with self.assertRaises(BoardError):
            self.b.create_task("t", "reviewer", "", ["done"], ["tech"])

    def test_only_owner_can_claim(self):
        t = self.task(["done"])
        with self.assertRaises(BoardError):
            self.b.add_claim("intel", t["id"], "done", "x", self.ev)

    def test_only_reviewer_verdict_counts(self):
        t = self.task(["done"])
        c = self.b.add_claim("tech", t["id"], "done", "x", self.ev)
        for who in ("chief", "tech", "human"):
            with self.assertRaises(BoardError):
                self.b.add_verdict(who, t["id"], c["id"], "accept", "ok")

    def test_evidence_type_rules(self):
        t = self.task(["done"])
        with self.assertRaises(BoardError):
            self.b.add_claim("tech", t["id"], "done", "x", [{"type": "text", "text": "我做完了"}])
        with self.assertRaises(BoardError):  # 命令不在白名单
            self.b.add_claim("tech", t["id"], "tested", "x", [{"type": "command", "cmd": "rm -rf /"}])
        with self.assertRaises(BoardError):  # 路径越界
            self.b.add_claim("tech", t["id"], "done", "x", [{"type": "file", "path": "../etc/passwd"}])

    def test_failed_command_is_auto_rejected_by_board(self):
        t = self.task(["tested"])
        c = self.b.add_claim("tech", t["id"], "tested", "测过了",
                             [{"type": "command", "cmd": "python -m unittest no_such_module"}])
        self.assertEqual(c["status"], "rejected")
        self.assertEqual(c["decided_by"], "board")
        self.assertEqual(c["verification"], "failed")

    def test_accept_then_close(self):
        t = self.task(["done"])
        c = self.b.add_claim("tech", t["id"], "done", "x", self.ev)
        claim, closed = self.b.add_verdict("reviewer", t["id"], c["id"], "accept", "ok")
        self.assertTrue(closed)
        self.assertEqual(self.b.task(t["id"])["status"], "closed")

    def test_reject_needs_reason(self):
        t = self.task(["done"])
        c = self.b.add_claim("tech", t["id"], "done", "x", self.ev)
        with self.assertRaises(BoardError):
            self.b.add_verdict("reviewer", t["id"], c["id"], "reject", "  ")

    def test_file_changed_before_accept_is_refused(self):
        t = self.task(["done"])
        c = self.b.add_claim("tech", t["id"], "done", "x", self.ev)
        self.f.write_text("changed", encoding="utf-8")
        with self.assertRaises(BoardError):
            self.b.add_verdict("reviewer", t["id"], c["id"], "accept", "ok")

    def test_file_changed_after_accept_invalidates_claim(self):
        t = self.task(["done", "pushed"])
        c1 = self.b.add_claim("tech", t["id"], "done", "x", self.ev)
        self.b.add_verdict("reviewer", t["id"], c1["id"], "accept", "ok")
        self.f.write_text("changed", encoding="utf-8")
        c2 = self.b.add_claim("tech", t["id"], "pushed", "x", [{"type": "text", "text": "commit abc123"}])
        self.b.add_verdict("reviewer", t["id"], c2["id"], "accept", "ok")
        self.b.approve(t["id"])
        self.assertEqual(self.b.task(t["id"])["status"], "open")  # done 已作废, 仍缺

    def test_text_only_claim_needs_human_approval(self):
        t = self.task(["pushed"])
        c = self.b.add_claim("tech", t["id"], "pushed", "x", [{"type": "text", "text": "commit abc123"}])
        self.assertEqual(c["verification"], "unverified")
        _, closed = self.b.add_verdict("reviewer", t["id"], c["id"], "accept", "ok")
        self.assertFalse(closed)
        self.assertEqual([x["id"] for x in self.b.waiting_human()], [t["id"]])
        self.assertTrue(self.b.approve(t["id"]))

    def test_new_claim_supersedes_old_pending(self):
        t = self.task(["done"])
        c1 = self.b.add_claim("tech", t["id"], "done", "x", self.ev)
        self.b.add_claim("tech", t["id"], "done", "y", self.ev)
        with self.assertRaises(BoardError):
            self.b.add_verdict("reviewer", t["id"], c1["id"], "accept", "ok")


class OfficeFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.office = Office(Path(self.tmp.name) / "ws", ROOT, MockBackend(), log=lambda *_: None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_permissions(self):
        o = self.office
        errs = o._execute("tech", {"actions": [{"type": "create_task", "title": "x", "owner": "tech"}]})
        self.assertTrue(any("无权" in e for e in errs))
        errs = o._execute("tech", {"actions": [{"type": "write_file", "path": "../intel/x.md", "content": "hi"}]})
        self.assertTrue(any("只能写入" in e for e in errs))
        errs = o._execute("tech", {"actions": [{"type": "message", "to": "intel", "text": "hi"}]})
        self.assertTrue(errs)
        errs = o._execute("chief", {"actions": [{"type": "final_report", "text": "x"}]})
        self.assertEqual(errs, [])  # 看板为空时允许
        errs = o._execute("chief", {"actions": [{"type": "create_task", "title": "x", "owner": ["tech", "intel"]}]})
        self.assertTrue(any("唯一" in e or "一个负责人" in e for e in errs))

    def test_end_to_end_with_mock(self):
        o = self.office
        o.give_goal("测试目标")
        self.assertEqual(o.run(60), "done")
        tasks = o.board.state["tasks"].values()
        self.assertEqual({t["status"] for t in tasks}, {"closed"})
        claims = [c for t in tasks for c in t["claims"]]
        self.assertTrue(any(c["decided_by"] == "board" and c["status"] == "rejected" for c in claims))
        self.assertTrue(any(c["decided_by"] == "reviewer" and c["status"] == "rejected" for c in claims))
        for role in ("chief", "tech", "intel", "creative", "reviewer"):  # 每个角色都有独立线程
            self.assertTrue((o.ws / "threads" / f"{role}.jsonl").exists(), role)
        self.assertFalse((o.ws / "threads" / "learning.jsonl").exists())
        report = (o.ws / "deliverables" / "final_report.md").read_text(encoding="utf-8")
        self.assertIn("T-003", report)

    def test_manual_mode_roundtrip(self):
        o = self.office
        o.give_goal("手动")
        self.assertIn("目标: 手动", o.inbox_text("chief"))
        raw = json.dumps({"say": "ok", "actions": [{"type": "create_task", "title": "t", "owner": "intel",
                                                     "spec": "s", "required": ["done"]}]})
        self.assertEqual(o.apply("chief", "```json\n" + raw + "\n```"), [])
        self.assertIn("新任务 T-001", o.inbox_text("intel"))


if __name__ == "__main__":
    unittest.main()
