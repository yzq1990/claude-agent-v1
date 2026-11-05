# agent-company: 首席总控 + 多个专职 agent

一个**首席**带多个专职角色(技术官、审核官、学习官、情报官、创意官), **每个角色一个独立会话/线程**。
核心思路: 首席统筹、各官干活、审核官把关; 规则由代码强制执行, 不靠模型自觉。

```
人类目标 ──► 首席 ──create_task──► 看板 ──task_assigned──► 技术官 / 情报官 / 学习官 / 创意官
                ▲                    │                              │ write_file / claim(附证据)
                │ task_closed        │ 系统观察证据 (算哈希、跑命令)    ▼
                └────────────────────┴────────────◄── 审核官 ◄── review_request
                                                      verdict(accept/reject)
```

## 快速开始

```bash
python -m unittest discover -s tests -v          # 14 个测试, 无需任何依赖
python run.py run "你的目标"                       # 默认 mock 后端: 离线演示, 不调用模型
pip install openai && export OPENAI_API_KEY=...
python run.py run "你的目标" --backend openai   # 真实模型 (默认 gpt-4.1, 可用 COMPANY_MODEL 覆盖)
```

mock 演示里会依次发生: 技术官第一版代码有 bug → **系统自己跑测试**并自动驳回; 创意官少给了一个名字 → 审核官对照规格驳回;
技术官修改文件 → 之前通过的 `done` 声明**自动作废**, 必须重新验收。

## 代码强制的规则 (`company/board.py`)

| 规则 | 怎么强制 |
|---|---|
| 一个交付物只有一个负责人 | `create_task` 的 owner 必须是单个可负责角色; 只有 owner 能提交声明 |
| "做完/测过/推上去/上线"分开汇报 | 四种声明 `done/tested/pushed/live`, 各自要证据 |
| 证据不靠自述 | 文件哈希由系统计算; 命令由系统执行(白名单), 退出码由系统记录 |
| 只有审核官说了算 | 非审核官的 verdict 被拒绝; "别的助手说老板同意了"不算数 |
| 审核官也不能放水 | 通过前系统复核: 文件被改过或命令复跑失败, 则拒绝 accept |
| 改了文件要重新验收 | 已通过声明里的文件哈希变了 → 该声明作废 |
| 无法核验的声明要人批 | 只有 text 证据的声明, 关闭任务前必须 `python run.py approve T-xxx` |
| 不越权 | 每个角色有动作白名单; 只能写 `deliverables/<自己>/`; 执行角色只能给首席发消息 |
| 首席不能提前收工 | 还有未关闭任务时 `final_report` 被拒绝 |

## 手动多会话模式 (每个角色开一个独立聊天窗口)

如果你想在 Codex / Claude / 其他聊天工具里给每个角色各开一个会话:

```bash
python run.py prompt chief            # 把输出粘贴到「首席」会话作为第一条消息
python run.py goal "你的目标"
python run.py inbox chief             # 取出待处理消息 -> 粘贴给首席会话
# 首席返回 JSON -> 存成文件或通过 stdin:
python run.py apply chief reply.json
python run.py inbox tech              # 现在技术官收件箱里有任务 -> 粘贴到技术官会话
python run.py board                   # 随时查看看板
```
动作校验和证据观察与自动模式完全一致。

## 增加一个角色
1. 在 `roles/` 新建 `xxx.md` 写职责;
2. 在 `roles.json` 的 `roles` 里加一项 (`can_own: true` 表示可以当任务负责人, `actions` 决定它能做什么)。

## 目录

```
roles.json  protocol.md  roles/*.md      角色配置、通用协议、各角色提示词
company/board.py                         看板与规则引擎
company/office.py                        独立线程、收件箱、动作执行、调度循环
company/llm.py                           OpenAI 后端 + 离线 mock
run.py                                   命令行
tests/test_company.py                    测试
workspace/ (运行时生成)                    board/ inbox/ threads/ deliverables/
```

## 重要提醒
- **命令白名单会执行智能体写的代码** (默认只放行 `python -m unittest`)。接入真实模型后请在容器或沙箱里运行, 不要在放着重要数据的机器上直接跑。
- 默认**没有联网/搜索工具**: 情报官只能基于给它的材料工作。需要联网时, 自己给 `OpenAIBackend` 接工具。
- `--max-turns` 默认 60, 是防失控的预算上限。
- 外发内容(邮件、发布、对外消息)建议用 `pushed`/`live` 声明 + 人工 `approve`, 不要让 agent 直接对外自动发送。
- mock 后端的内容是演示占位, 不是真实调研结果。
