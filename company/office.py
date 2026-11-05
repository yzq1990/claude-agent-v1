"""办公室: 每个角色一个独立线程(会话)和收件箱; 首席通过看板分派任务; 审核官把关。

一个"回合"(turn) = 取出某角色收件箱里的消息 -> 连同该角色自己的历史发给模型
-> 解析模型返回的 JSON 动作 -> 由系统校验权限并执行。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .board import Board, BoardError, now

MAX_FILE_BYTES = 200_000


def extract_json(text: str) -> dict:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j <= i:
            raise ValueError("回复中没有 JSON 对象")
        t = t[i : j + 1]
    obj = json.loads(t)
    if not isinstance(obj, dict):
        raise ValueError("顶层必须是 JSON 对象")
    return obj


def render(m: dict) -> str:
    return f"[来自 {m['from']} | {m['event']}]\n{m['text']}"


class Office:
    def __init__(self, workspace, project_dir, backend=None, log=print):
        self.ws = Path(workspace).resolve()
        self.proj = Path(project_dir).resolve()
        self.backend = backend
        self.log = log
        cfg = json.loads((self.proj / "roles.json").read_text(encoding="utf-8"))
        self.roles = cfg["roles"]
        self.chief, self.reviewer = cfg["chief"], cfg["reviewer"]
        self.max_history = int(cfg.get("max_history", 40))
        self.protocol = (self.proj / "protocol.md").read_text(encoding="utf-8")
        for d in ("inbox", "threads", "deliverables"):
            (self.ws / d).mkdir(parents=True, exist_ok=True)
        self.board = Board(self.ws, cfg.get("allowed_commands", []), self.reviewer)
        self.turns = 0

    # ---------- 提示词 ----------
    def system_prompt(self, role: str) -> str:
        cfg = self.roles[role]
        body = (self.proj / cfg["prompt"]).read_text(encoding="utf-8")
        roster = "\n".join(f"- {r} ({c['name']}): {c['desc']}" for r, c in self.roles.items())
        return (f"你是「{cfg['name']}」, 角色 id 是 `{role}`。\n\n{body}\n\n---\n# 公司通用协议\n{self.protocol}\n\n"
                f"# 成员名册\n{roster}\n\n# 你被允许的动作类型\n{cfg['actions']}")

    # ---------- 收件箱 ----------
    def _inbox(self, role):
        return self.ws / "inbox" / f"{role}.json"

    def send(self, to, frm, event, text, data=None):
        if to not in self.roles:
            raise BoardError(f"未知角色: {to} (可选: {list(self.roles)})")
        p = self._inbox(to)
        q = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        q.append({"from": frm, "event": event, "text": text, "data": data or {}, "at": now()})
        p.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")

    def pending(self, role) -> int:
        p = self._inbox(role)
        return len(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else 0

    def _pop(self, role):
        p = self._inbox(role)
        q = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        p.write_text("[]", encoding="utf-8")
        return q

    # ---------- 独立线程 ----------
    def _thread(self, role):
        return self.ws / "threads" / f"{role}.jsonl"

    def _history(self, role):
        p = self._thread(role)
        if not p.exists():
            return []
        msgs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        msgs = msgs[-self.max_history :]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    def _append(self, role, who, content):
        with open(self._thread(role), "a", encoding="utf-8") as f:
            f.write(json.dumps({"role": who, "content": content}, ensure_ascii=False) + "\n")

    # ---------- 回合 ----------
    def give_goal(self, goal: str):
        final = self.ws / "deliverables" / "final_report.md"
        if final.exists():
            final.rename(final.with_name(f"final_report.{now().replace(':', '').replace(' ', '_')}.md"))
        self.send(self.chief, "human", "goal", f"目标: {goal}", {"goal": goal})

    def turn(self, role: str) -> bool:
        msgs = self._pop(role)
        if not msgs:
            return False
        user = "\n\n".join(render(m) for m in msgs)
        if role == self.chief:
            user += "\n\n## 当前看板\n" + self.board.summary()
        raw = self.backend.complete(
            system=self.system_prompt(role), history=self._history(role), user=user,
            meta={"role": role, "messages": msgs, "board": self.board.state, "workspace": str(self.ws)})
        self._append(role, "user", user)
        self._append(role, "assistant", raw)
        self.turns += 1
        try:
            obj = extract_json(raw)
        except Exception as e:  # noqa: BLE001
            self.log(f"[#{self.turns}] {self.roles[role]['name']} 回复格式错误: {e}")
            self.send(role, "system", "format_error", f"你的上一条回复不是合法 JSON ({e})。请只输出一个 JSON 对象。")
            return True
        errors = self._execute(role, obj)
        n = len(obj.get("actions") or [])
        self.log(f"[#{self.turns}] {self.roles[role]['name']}: 收到 {len(msgs)} 条消息, 执行 {n} 个动作"
                 + (f", {len(errors)} 个被拒绝" if errors else ""))
        if obj.get("say"):
            self.log(f"        「{str(obj['say'])[:100]}」")
        for e in errors:
            self.log(f"        ✗ {e}")
        return True

    def _execute(self, role: str, obj: dict):
        errors = []
        for i, act in enumerate(obj.get("actions") or [], 1):
            try:
                if not isinstance(act, dict):
                    raise BoardError("动作必须是对象")
                typ = act.get("type")
                if typ not in self.roles[role]["actions"]:
                    raise BoardError(f"{role} 无权执行动作 {typ!r}")
                getattr(self, f"_act_{typ}")(role, act)
            except KeyError as e:
                errors.append(f"动作#{i} 缺少字段 {e}")
            except (BoardError, ValueError, TypeError) as e:
                errors.append(f"动作#{i} ({act.get('type') if isinstance(act, dict) else act}): {e}")
        if errors:
            self.send(role, "system", "action_error", "以下动作被系统拒绝, 请修正:\n- " + "\n- ".join(errors))
        return errors

    # ---------- 动作 ----------
    def _act_create_task(self, role, a):
        owners = [r for r, c in self.roles.items() if c.get("can_own")]
        t = self.board.create_task(a["title"], a["owner"], a.get("spec", ""), a.get("required"), owners)
        self.send(t["owner"], role, "task_assigned",
                  f"新任务 {t['id']}: {t['title']}\n规格: {t['spec']}\n需要提交的声明类型: {', '.join(t['required'])}",
                  {"task": t})

    def _act_cancel_task(self, role, a):
        self.board.cancel(a["task_id"])
        self._maybe_all_closed()

    def _act_message(self, role, a):
        to = a["to"]
        if role not in (self.chief, self.reviewer) and to != self.chief:
            raise BoardError("执行角色只能给首席发消息")
        self.send(to, role, "message", str(a["text"]))

    def _act_final_report(self, role, a):
        open_ = self.board.open_tasks()
        if open_:
            raise BoardError("还有未关闭的任务, 不能提交最终报告: " + ", ".join(t["id"] for t in open_))
        (self.ws / "deliverables" / "final_report.md").write_text(str(a["text"]), encoding="utf-8")

    def _act_write_file(self, role, a):
        base = (self.ws / "deliverables" / role).resolve()
        p = (base / str(a["path"])).resolve()
        if base not in p.parents:
            raise BoardError(f"只能写入 deliverables/{role}/ 之内: {a['path']}")
        content = str(a["content"])
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise BoardError("文件过大")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def _act_claim(self, role, a):
        evid = [dict(e) for e in a["evidence"] if isinstance(e, dict)]
        for e in evid:  # 允许写成相对于自己目录的路径
            if e.get("type") == "file" and not str(e.get("path", "")).startswith("deliverables/"):
                e["path"] = f"deliverables/{role}/{e.get('path', '')}"
        claim = self.board.add_claim(role, a["task_id"], a["kind"], a.get("summary", ""), evid)
        t = self.board.task(a["task_id"])
        if claim["status"] == "rejected":  # 系统自动驳回: 命令失败
            self.send(role, "board", "claim_rejected",
                      f"系统自动驳回 {claim['id']} ({claim['kind']}): {claim['reason']}",
                      {"task": t, "claim": claim, "decided_by": "board", "reason": claim["reason"]})
        else:
            self.send(self.reviewer, "board", "review_request", self._review_text(t, claim),
                      {"task": t, "claim": claim})

    def _review_text(self, t, c):
        lines = [f"请审核任务 {t['id']}「{t['title']}」的声明 {c['id']} (类型: {c['kind']}, 负责人: {t['owner']})。",
                 f"任务规格: {t['spec']}", f"负责人的说明: {c['summary']}",
                 "系统观察到的证据 (由系统而非负责人产生):"]
        for e in c["evidence"]:
            if e["type"] == "file":
                body = (self.ws / e["path"]).read_text(encoding="utf-8", errors="replace")[:3000]
                lines.append(f"- 文件 {e['path']} sha256={e['sha256'][:12]} {e['bytes']}B\n```\n{body}\n```")
            elif e["type"] == "command":
                lines.append(f"- 命令 `{e['cmd']}` 退出码={e['exit_code']}\n  输出尾部: {e['output_tail']}")
            else:
                lines.append(f"- 文字说明(未经核验): {e['text']}")
        lines.append("请逐条对照任务规格给出 verdict; 不要因为负责人自述而通过。")
        return "\n".join(lines)

    def _act_verdict(self, role, a):
        claim, closed = self.board.add_verdict(role, a["task_id"], a["claim_id"], a["decision"], a.get("reason", ""))
        t = self.board.task(a["task_id"])
        ok = claim["status"] == "accepted"
        self.send(t["owner"], role, "claim_accepted" if ok else "claim_rejected",
                  f"审核官{'通过' if ok else '驳回'}了 {claim['id']} ({claim['kind']}): {claim['reason']}",
                  {"task": t, "claim": claim, "decided_by": role, "reason": claim["reason"]})
        if closed:
            self.send(self.chief, "board", "task_closed", f"{t['id']} 已关闭: {t['title']}")
        self._maybe_all_closed()

    def _maybe_all_closed(self):
        tasks = list(self.board.state["tasks"].values())
        if not tasks or self.board.open_tasks():
            return
        sig = ",".join(sorted(f"{t['id']}:{t['status']}" for t in tasks))
        if self.board.state.get("all_closed_sig") == sig:
            return
        self.board.state["all_closed_sig"] = sig
        self.board.save()
        self.send(self.chief, "board", "all_tasks_closed", "看板上所有任务都已关闭或取消, 请汇总并提交 final_report。")

    # ---------- 人工 ----------
    def approve(self, task_id):
        closed = self.board.approve(task_id)
        if closed:
            t = self.board.task(task_id)
            self.send(self.chief, "human", "task_closed", f"{task_id} 已获人工批准并关闭: {t['title']}")
            self._maybe_all_closed()
        return closed

    # ---------- 手动多会话模式 ----------
    def inbox_text(self, role):
        """取出并渲染某角色的待处理消息, 供你粘贴到该角色的独立聊天会话里。"""
        msgs = self._pop(role)
        if not msgs:
            return ""
        text = "\n\n".join(render(m) for m in msgs)
        if role == self.chief:
            text += "\n\n## 当前看板\n" + self.board.summary()
        return text

    def apply(self, role, raw):
        """把某角色在外部会话里返回的 JSON 动作交给系统执行。"""
        return self._execute(role, extract_json(raw))

    # ---------- 主循环 ----------
    def final_exists(self):
        return (self.ws / "deliverables" / "final_report.md").exists()

    def run(self, max_turns=60):
        start = self.turns
        while self.turns - start < max_turns:
            if self.final_exists():
                return "done"
            progressed = False
            for role in self.roles:
                if self.pending(role):
                    self.turn(role)
                    progressed = True
                    if self.final_exists():
                        break
            if not progressed:
                break
        if self.final_exists():
            return "done"
        if self.board.waiting_human():
            return "waiting_human"
        if any(self.pending(r) for r in self.roles):
            return "budget"
        return "stalled"

    def reset(self):
        shutil.rmtree(self.ws, ignore_errors=True)
