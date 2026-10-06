<div align="center">

<img src="./asset/logo/lupuslogo2.png" alt="LUPUS — AI works together" width="380" />

# LUPUS

**AI WORKS TOGETHER**

### 让 Claude Code 与 Codex CLI 把可验证的工作真正做完的本地 supervisor —<br/>以证据判定完成 · 预算 · 崩溃恢复 · Claude ↔ Codex 交接

[English](./README.md) · [한국어](./README.ko.md) · [简体中文](./README.zh-CN.md) · [日本語](./README.ja.md)

[![License](https://img.shields.io/badge/license-Apache--2.0-1f2937?style=flat-square)](./LICENSE)
![Stage](https://img.shields.io/badge/stage-v0.1%20alpha-d69526?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square)
![Dependencies](https://img.shields.io/badge/runtime%20deps-0-2ea043?style=flat-square)
![Tests](https://img.shields.io/badge/offline%20tests-224%20passing-2ea043?style=flat-square)

</div>

Lupus 把你已经安装并登录的 `claude` 和 `codex` CLI（沿用现有订阅登录）作为 worker 来运行，而“完成”由它自己判定：只有确定性检查通过，目标才算完成，而不是因为模型说“做好了”。目标、预算、尝试、检查点和项目知识都保存在本地 SQLite 中，因此在崩溃、额度用尽或更换 AI 之后，工作仍能继续。

**当前为 `v0.1 alpha`。** 这是 macOS 上的单用户工具，没有沙箱（worker 以你的用户权限运行），下面的测量规模也很小。请勿用于敏感资料。

## 功能

| | |
|---|---|
| **只凭证据完成** | DONE 需要与当前验收条件绑定的检查通过。worker 说“修好了”不算证据。 |
| **冻结测试** | 在 worker 启动前冻结测试文件和运行器配置。被修改、删除或跳过的测试会在验证前被还原。 |
| **Claude ↔ Codex 交接** | 一个 AI 停下时（额度、崩溃或你的选择），另一个从已验证的检查点继续。已完成的步骤不会重做，预算与尝试次数不会被重置。 |
| **预算与防空转** | 调用、尝试、时间和 token 在开始前预留。重复同一次失败的尝试会在调用模型之前被拒绝。 |
| **崩溃安全的检查点** | 恢复对象先持久化，再提交数据库；已通过在每个边界杀死进程的测试。 |
| **轻量启动** | worker 启动时不加载任务不需要的插件、钩子、MCP 和技能说明，并把任务文件直接放进 prompt。 |
| **记忆图谱** | 项目知识以带来源的类型化节点和链接保存。每次召回都绑定到一次尝试，并按该尝试的验证结果评分。 |
| **失败测试一条命令** | `lupus fix-tests` 不需要目标文件：supervisor 自己观察到测试失败，这个失败就是目标。 |

## 在一台 Mac 上的测量（2026-10-05）

相同任务、相同检查、每次运行使用全新目录。Claude Code 2.1.289，codex-cli 0.160.0。所有运行都通过了检查。token = 新输入 + 缓存输入 + 输出。

| 场景 | 按你平时配置运行的 CLI | Lupus |
|---|---|---|
| 3 个一次完成的编码任务，Claude | 135,729 token · 15.0 秒 | 12,166 token · 6.2 秒 |
| 3 个一次完成的编码任务，Codex | 88,530 token · 26.2 秒 | 36,298 token · 12.2 秒 |
| 4 步项目，Claude | 298,327 token · 52.0 秒 | 70,768 token · 42.5 秒 |
| 同一项目，中断后由另一个 AI 接手 | 337,133 token · 82.0 秒 · 重新说明 1,139 字符 | 138,989 token · 64.1 秒 · 无需说明 |
| 修复失败测试，Claude | 136,712 token · 16.2 秒 | 12,817 token · 5.7 秒 · 一条命令 |

请如实解读：

- **节省主要来自不加载不需要的配置，以及 prompt 技巧（文件随 prompt 提供、一次写完）。** 不使用 Lupus、仅用同样技巧的 CLI 调用，在一次完成的任务上消耗了相同的 token。此时 supervisor 带来的是“经过验证的完成”，而不是更便宜的 token。
- 定义带检查的目标，输入量约为普通 prompt 的 1.9 倍（`fix-tests` 除外）。
- 在中断场景中，Codex 阶段比普通 CLI **多用了 33%**：Lupus 每一步调用一次，而 Codex 每次调用的固定输入很大。
- 每格只有 1–2 次运行。在 3 个带隐藏 holdout 测试的更难任务上，36 次运行全部通过且没有误通过，因此该基准无法证明冻结测试能减少虚假完成。

原始数据与脚本：[`lupus/docs/`](./lupus/docs) · [`lupus/evaluations/`](./lupus/evaluations) · 完整记录见 [docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md)（韩语）。

## 何时使用，何时不用

**适合使用**：可以由程序检查的工作——失败的测试、带测试的多步任务、可能被中断或需要在 Claude 与 Codex 之间切换的长任务、有值得记录一次的约定的项目。

**用普通 CLI 更好**：一次性提问、边聊边探索的工作、需要插件或 MCP 的任务，以及无法由程序判定的工作（规划、调研、写作、设计）。

## 快速开始

环境要求：macOS；Python **3.12+**，其 SQLite 为 **3.51.3+ 且启用 FTS5**（Homebrew Python 即可）；已安装并登录 `claude` 和/或 `codex`。

```bash
git clone https://github.com/djfksjd/lupus.git
cd lupus/lupus
python3 -m pip install -e .          # 或：export PYTHONPATH=src 后使用 `python3 -m lupus`

lupus init                           # 创建 ~/.lupus（数据库 + vault）
lupus probe --live                   # 测量已安装 CLI 实际支持的范围（2 次很小的调用）

cd ~/work/my-project                 # 一个有失败测试的项目
lupus fix-tests --driver claude      # 观察失败 -> 冻结测试 -> 修复 -> 验证
lupus do "给导出命令增加 --json 选项" --driver claude
                                     # 任意请求：先写出会失败的测试 -> 你审阅批准 -> 再实现
```

带自定义检查的目标：

```bash
lupus project-add ~/work/site --name site --providers anthropic,openai
lupus goal-submit <project_id> goal.json
lupus run <goal_id> --driver claude
lupus status <goal_id>               # 状态、预算、能否恢复及原因
lupus run <goal_id> --driver codex   # 换另一个 AI 继续（经过验证的交接）
lupus graph --open                   # 知识图谱视图
```

`goal.json` 格式与全部命令见 [`lupus/README.md`](./lupus/README.md)（韩语）。

## 工作原理

```text
 you ──► goal + acceptance checks ──► ┌──────────────── Lupus supervisor (plain program) ───────────────┐
                                      │ budget · attempts · leases · checkpoints · frozen tests · memory │
                                      └───────┬───────────────────────────────────────────────┬─────────┘
                                   lean prompt│                                               │verify (deterministic)
                                              ▼                                               ▼
                                   claude  ◄──handoff──►  codex                    PASS evidence ──► DONE
```

- supervisor 是普通程序。路由、预算、重试和完成都不由模型调用决定。
- worker 只得到 prompt 和一个目录，拿不到数据库、CLI 或任何权限。
- 不修改任何全局设置：不装钩子、不改 PATH、不动 `~/.claude` 和 `~/.codex`。

<div align="center">
<img src="./asset/screenshots/graph.png" alt="Lupus 知识图谱视图" width="760" />
<br/><sub>知识图谱视图（<code>lupus graph</code>）：节点、类型化链接，以及知识被记录和被召回的目标</sub>
</div>

## 需要了解的限制

- **没有虚拟机或容器隔离。** 验证器在 macOS 沙箱中运行，worker 由各 CLI 自身的限制功能约束，但 CLI 本身以你的身份运行，不要交给它受保护或客户的数据。读取范围按各 CLI 所能支持的程度进行限制，并用 canary 文件实测：Codex 以隐藏主目录、命令无网络的权限配置启动。限制未被验证的 driver 需要按项目显式授权。详见 [SECURITY.md](./SECURITY.md)。
- 未接入你平时的 `claude` / `codex` 会话，需要显式运行 `lupus`；没有交互模式。
- `fix-tests` 仅支持 Python `unittest`/`pytest` 项目。
- 代码还很年轻。八轮外部评审发现并修复了 60 个缺陷，应当假定仍有遗漏。
- 无法观测订阅额度的实际消耗；token 数为 CLI 报告的数值。

## 文档

- [机器契约](./lupus/docs/CONTRACT.md) — 代码强制执行的规则，以及范围之外的内容（韩语）
- [实现记录](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md) — 决策、评审、所有测量及其局限（韩语）
- [设计](./docs/design/LUPUS-PLAN.md) — 完整设计（韩语）
- [第三方声明](./lupus/THIRD_PARTY_NOTICES.md) — 图谱视图原样打包了 vis-network

## 许可证

[Apache-2.0](./LICENSE)。打包的 vis-network 按 MIT 许可使用。
