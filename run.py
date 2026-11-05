#!/usr/bin/env python3
"""agent-company 命令行。

自动模式:  python run.py run "你的目标" [--backend mock|openai]
手动模式:  prompt / goal / inbox / apply (每个角色用一个独立的聊天会话, 你来回搬运)
人工把关:  approve <任务ID>
"""
import argparse
import sys
from pathlib import Path

from company.board import BoardError
from company.llm import OpenAIBackend, MockBackend
from company.office import Office

ROOT = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default="workspace", help="运行数据目录 (默认 ./workspace)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("run", "resume"):
        p = sub.add_parser(name, help="自动运行" if name == "run" else "继续运行 (例如人工批准之后)")
        if name == "run":
            p.add_argument("goal", help="交给首席的目标")
        p.add_argument("--backend", choices=["mock", "openai"], default="mock")
        p.add_argument("--max-turns", type=int, default=60, help="回合上限, 防止失控烧钱")

    sub.add_parser("board", help="查看看板")
    p = sub.add_parser("approve", help="人工批准任务 (需要人工批准的任务才会卡在这一步)")
    p.add_argument("task_id")
    p = sub.add_parser("prompt", help="打印某角色完整的系统提示词 (粘贴到独立会话里)")
    p.add_argument("role")
    p = sub.add_parser("goal", help="手动模式: 把目标放进首席的收件箱")
    p.add_argument("text")
    p = sub.add_parser("inbox", help="手动模式: 取出某角色的待处理消息")
    p.add_argument("role")
    p = sub.add_parser("apply", help="手动模式: 执行某角色返回的 JSON (文件路径, 或 - 表示 stdin)")
    p.add_argument("role")
    p.add_argument("file", nargs="?", default="-")
    p = sub.add_parser("reset", help="清空 workspace")
    p.add_argument("--yes", action="store_true")
    a = ap.parse_args()

    backend = None
    if a.cmd in ("run", "resume"):
        backend = MockBackend() if a.backend == "mock" else OpenAIBackend()
        if a.backend == "mock":
            print("[mock 后端: 离线演示, 不调用任何模型]")
    office = Office(a.workspace, ROOT, backend)

    try:
        if a.cmd in ("run", "resume"):
            if a.cmd == "run":
                office.give_goal(a.goal)
            status = office.run(a.max_turns)
            print(f"\n状态: {status}")
            print(office.board.summary())
            if status == "done":
                print(f"最终报告: {office.ws / 'deliverables' / 'final_report.md'}")
            elif status == "waiting_human":
                for t in office.board.waiting_human():
                    print(f"等待人工批准: python run.py approve {t['id']}  (然后 python run.py resume)")
        elif a.cmd == "board":
            print(office.board.summary())
        elif a.cmd == "approve":
            closed = office.approve(a.task_id)
            print(f"{a.task_id} 已批准" + ("并关闭" if closed else " (仍缺少其他已通过的声明)"))
        elif a.cmd == "prompt":
            print(office.system_prompt(a.role))
        elif a.cmd == "goal":
            office.give_goal(a.text)
            print("已放入首席收件箱。下一步: python run.py inbox chief")
        elif a.cmd == "inbox":
            print(office.inbox_text(a.role) or "(没有待处理消息)")
        elif a.cmd == "apply":
            raw = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
            errors = office.apply(a.role, raw)
            print("已执行。" if not errors else "部分动作被拒绝:\n- " + "\n- ".join(errors))
        elif a.cmd == "reset":
            if not a.yes:
                sys.exit("加 --yes 确认清空 workspace")
            office.reset()
            print("已清空")
    except (BoardError, KeyError, KeyboardInterrupt) as e:
        sys.exit(f"错误: {e}")


if __name__ == "__main__":
    main()
