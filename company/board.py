"""看板: 任务 / 声明(claim) / 证据 / 审核状态。

规则全部用代码强制, 不依赖模型自觉:
  1. 每个任务只有一个负责人, 只有负责人能提交声明
  2. 证据由系统观察: 文件哈希由系统计算, 命令由系统执行; 负责人的自述不算证据
  3. 只有审核官的裁决有效; 通过前系统会再次复核证据
  4. 文件在通过后被改动, 对应声明作废, 需要重新验收
  5. 只有文字证据(无法核验)的声明, 关闭任务前还需要人工批准
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

# 每种声明至少要有下列证据类型之一
KIND_RULES = {
    "done": {"file"},
    "tested": {"command"},
    "pushed": {"command", "text"},
    "live": {"command", "text"},
}
DEFAULT_REQUIRED = ["done", "tested"]


class BoardError(Exception):
    pass


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Board:
    def __init__(self, workspace, allowed_commands=(), reviewer_id="reviewer"):
        self.ws = Path(workspace).resolve()
        self.file = self.ws / "board" / "board.json"
        self.allowed = list(allowed_commands)
        self.reviewer_id = reviewer_id
        self.file.parent.mkdir(parents=True, exist_ok=True)
        if self.file.exists():
            self.state = json.loads(self.file.read_text(encoding="utf-8"))
        else:
            self.state = {"next_task": 1, "next_claim": 1, "tasks": {}}
            self.save()

    # ---------- 基础 ----------
    def save(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.file)

    def task(self, tid):
        t = self.state["tasks"].get(tid)
        if not t:
            raise BoardError(f"没有这个任务: {tid}")
        return t

    def open_tasks(self):
        return [t for t in self.state["tasks"].values() if t["status"] == "open"]

    def _resolve(self, rel) -> Path:
        p = (self.ws / str(rel)).resolve()
        if p != self.ws and self.ws not in p.parents:
            raise BoardError(f"路径越界: {rel}")
        return p

    def _run_command(self, cmd):
        try:
            parts = shlex.split(str(cmd))
        except ValueError as e:
            raise BoardError(f"命令无法解析: {e}")
        if not parts:
            raise BoardError("命令为空")
        ok = False
        for a in self.allowed:
            ap = shlex.split(a)
            if parts[: len(ap)] == ap:
                ok = True
        if not ok:
            raise BoardError(f"命令不在白名单: {cmd} (白名单: {self.allowed})")
        argv = [sys.executable if parts[0] in ("python", "python3") else parts[0]] + parts[1:]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        try:
            r = subprocess.run(argv, cwd=self.ws, capture_output=True, text=True, timeout=120, env=env)
            return r.returncode, (r.stdout + r.stderr)[-800:]
        except subprocess.TimeoutExpired:
            return 124, "超时(120s)"
        except FileNotFoundError as e:
            return 127, str(e)

    # ---------- 任务 ----------
    def create_task(self, title, owner, spec="", required=None, owners_allowed=()):
        if not isinstance(owner, str):
            raise BoardError("一个交付物只能有一个负责人 (owner 必须是单个角色 id)")
        if owner not in owners_allowed:
            raise BoardError(f"{owner} 不能作为负责人; 可选: {list(owners_allowed)}")
        if not str(title).strip():
            raise BoardError("任务必须有标题")
        required = DEFAULT_REQUIRED if required is None else required
        if not isinstance(required, list) or not required:
            raise BoardError("required 必须是非空列表")
        bad = [k for k in required if k not in KIND_RULES]
        if bad:
            raise BoardError(f"未知的声明类型: {bad}; 可用: {list(KIND_RULES)}")
        tid = f"T-{self.state['next_task']:03d}"
        self.state["next_task"] += 1
        t = {"id": tid, "title": str(title), "owner": owner, "spec": str(spec), "required": list(required),
             "status": "open", "claims": [], "needs_human": False, "human_approved": False, "created": now()}
        self.state["tasks"][tid] = t
        self.save()
        return t

    def cancel(self, task_id):
        t = self.task(task_id)
        if t["status"] != "open":
            raise BoardError(f"{task_id} 已是 {t['status']}")
        t["status"] = "cancelled"
        self.save()

    # ---------- 声明与证据 ----------
    def add_claim(self, by, task_id, kind, summary, evidence):
        t = self.task(task_id)
        if t["status"] != "open":
            raise BoardError(f"{task_id} 已是 {t['status']}, 不能再提交声明")
        if by != t["owner"]:
            raise BoardError(f"只有负责人 {t['owner']} 能为 {task_id} 提交声明")
        if kind not in KIND_RULES:
            raise BoardError(f"未知的声明类型: {kind}; 可用: {list(KIND_RULES)}")
        if not isinstance(evidence, list) or not evidence:
            raise BoardError("声明必须附证据")
        obs = []
        for ev in evidence:
            typ = ev.get("type") if isinstance(ev, dict) else None
            if typ == "file":
                p = self._resolve(ev.get("path", ""))
                if not p.is_file():
                    raise BoardError(f"证据文件不存在: {ev.get('path')}")
                obs.append({"type": "file", "path": ev["path"], "sha256": sha256_file(p),
                            "bytes": p.stat().st_size, "observed_by": "board"})
            elif typ == "command":
                code, out = self._run_command(ev.get("cmd", ""))
                obs.append({"type": "command", "cmd": ev["cmd"], "exit_code": code,
                            "output_tail": out, "observed_by": "board"})
            elif typ == "text":
                obs.append({"type": "text", "text": str(ev.get("text", ""))[:2000],
                            "observed_by": "agent(unverified)"})
            else:
                raise BoardError(f"未知的证据类型: {typ} (可用: file / command / text)")
        if not any(e["type"] in KIND_RULES[kind] for e in obs):
            raise BoardError(f"'{kind}' 声明至少需要这些证据类型之一: {sorted(KIND_RULES[kind])}")
        failed = [e for e in obs if e["type"] == "command" and e["exit_code"] != 0]
        if failed:
            verification = "failed"
        elif any(e["type"] in ("file", "command") for e in obs):
            verification = "verified"
        else:
            verification = "unverified"
        for c in t["claims"]:  # 同类型的旧待审声明作废
            if c["kind"] == kind and c["status"] == "pending":
                c["status"] = "superseded"
        cid = f"C-{self.state['next_claim']:03d}"
        self.state["next_claim"] += 1
        claim = {"id": cid, "kind": kind, "by": by, "summary": str(summary), "evidence": obs,
                 "verification": verification, "status": "pending", "reason": "", "decided_by": None,
                 "created": now()}
        if failed:
            claim["status"] = "rejected"
            claim["decided_by"] = "board"
            claim["reason"] = "; ".join(
                f"系统执行 `{e['cmd']}` 退出码 {e['exit_code']}: {e['output_tail'][-400:]}" for e in failed)
        t["claims"].append(claim)
        self.save()
        return claim

    def _files_intact(self, claim):
        for e in claim["evidence"]:
            if e["type"] == "file":
                p = self._resolve(e["path"])
                if not p.is_file() or sha256_file(p) != e["sha256"]:
                    return False
        return True

    def _reverify(self, claim):
        probs = []
        for e in claim["evidence"]:
            if e["type"] == "file":
                p = self._resolve(e["path"])
                if not p.is_file():
                    probs.append(f"文件已不存在: {e['path']}")
                elif sha256_file(p) != e["sha256"]:
                    probs.append(f"文件在声明后被改动: {e['path']} (请负责人重新提交声明)")
            elif e["type"] == "command":
                code, out = self._run_command(e["cmd"])
                if code != 0:
                    probs.append(f"复跑 `{e['cmd']}` 失败, 退出码 {code}: {out[-200:]}")
        return probs

    # ---------- 审核与关闭 ----------
    def add_verdict(self, by, task_id, claim_id, decision, reason=""):
        if by != self.reviewer_id:
            raise BoardError(f"只有审核官({self.reviewer_id})的裁决有效; {by} 的批准或驳回不算数")
        t = self.task(task_id)
        claim = next((c for c in t["claims"] if c["id"] == claim_id), None)
        if not claim:
            raise BoardError(f"{task_id} 里没有声明 {claim_id}")
        if claim["status"] != "pending":
            raise BoardError(f"{claim_id} 当前状态是 {claim['status']}, 不能再裁决")
        if decision not in ("accept", "reject"):
            raise BoardError("decision 必须是 accept 或 reject")
        if decision == "reject" and not str(reason).strip():
            raise BoardError("驳回必须写明理由")
        if decision == "accept":
            probs = self._reverify(claim)
            if probs:
                raise BoardError("系统复核失败, 不能通过: " + "; ".join(probs))
        claim["status"] = "accepted" if decision == "accept" else "rejected"
        claim["decided_by"] = by
        claim["reason"] = str(reason)
        closed = self._try_close(t)
        self.save()
        return claim, closed

    def _missing(self, t):
        missing, needs_human = [], t.get("needs_human", False)
        for kind in t["required"]:
            ok = [c for c in t["claims"]
                  if c["kind"] == kind and c["status"] == "accepted" and self._files_intact(c)]
            if not ok:
                missing.append(kind)
            elif not any(c["verification"] == "verified" for c in ok):
                needs_human = True
        t["needs_human"] = needs_human
        return missing

    def _try_close(self, t):
        if t["status"] != "open" or self._missing(t):
            return False
        if t["needs_human"] and not t["human_approved"]:
            return False
        t["status"] = "closed"
        t["closed"] = now()
        return True

    def approve(self, task_id):
        """人工批准 (只能通过 CLI 调用, 智能体没有这个动作)。"""
        t = self.task(task_id)
        if t["status"] != "open":
            raise BoardError(f"{task_id} 已是 {t['status']}")
        t["human_approved"] = True
        closed = self._try_close(t)
        self.save()
        return closed

    def waiting_human(self):
        res = [t for t in self.open_tasks()
               if not self._missing(t) and t["needs_human"] and not t["human_approved"]]
        self.save()
        return res

    # ---------- 展示 ----------
    def summary(self):
        lines = []
        for t in self.state["tasks"].values():
            claims = ", ".join(f"{c['id']}:{c['kind']}:{c['status']}"
                               for c in t["claims"] if c["status"] != "superseded")
            waiting = t["status"] == "open" and t["needs_human"] and not t["human_approved"]
            extra = " | 等待人工批准" if waiting else ""
            lines.append(f"{t['id']} [{t['status']}] 负责人={t['owner']} 需要={'/'.join(t['required'])} "
                         f"{t['title']} | 声明: {claims or '无'}{extra}")
        return "\n".join(lines) or "(看板为空)"
