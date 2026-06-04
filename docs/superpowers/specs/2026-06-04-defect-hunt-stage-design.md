# Design: `defect_hunt` 阶段 — 将 chaos-qa-hunter 内化进产品

> 日期：2026-06-04 · 作者：zmy + Claude Code
> 来源依据：
> - skill 原文 `~/.claude/skills/chaos-qa-hunter/SKILL.md`（395 行）
> - 已验证的手动跑法 `outputs/_cc_selfhunt/`（COMPARISON.md：self-hunt F1 0.157 vs 产品 0.095）
> - 现有 detection 实现 `eval/prompt/defect_detection.py` + `eval/agent/claude_code.py`

## 1. 问题与目标

`chaos-qa-hunter` 是一个 **Claude Code skill**，住在 `~/.claude/skills/`。当 WebTestBench
在别人的机器上运行时该 skill 不存在，所以它的"白盒锚定对抗法"无法被产品复用。

目标：把该方法论**固化成仓库内的一个 prompt + 一个流水线阶段**（像现有 `defect_detection.py`
那样由 harness 注入），让产品自身具备"脱离 checklist 的自由对抗找 bug"能力，且**不依赖任何
本地 skill**。

### 现状瓶颈（为什么需要它）

现有 `defect_detection` 是 **checklist 驱动**：agent 只验证 gold-derived checklist 里的条目，
召回被 checklist 框死（已知 checklist 漏 ~43% gold bug）。chaos 方法读全部源码、建攻击面图、
按八类向量系统性攻击、循环到覆盖率达标 —— 能发现 checklist 从未提及的缺陷。

## 2. 关键决策（已与用户确认）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| 1 | 与现有 detection 的关系 | **新增独立 `defect_hunt` 阶段** | 与 `_cc_selfhunt` 手动做法一致，已验证有效；不动现有路径 |
| 2 | 与 scoring 的关系 | **双轨并行：hunt 不进 scoring** | hunt 只产人读面 `BUGS.md`；规避"自由发现被 gold 误判成 FP"的精度风险（0002 教训） |
| 3 | 多轮循环保真度 | **有限多轮（封顶 N 轮 + 覆盖率快照 + 早停）** | 保留 skill 灵魂又不烧穿预算 |
| 4 | 多轮实现位置 | **C1：单 SDK session 内自循环** | `_cc_selfhunt` 实际就是单 session 跑的；预算由 `max_turns` 硬封顶 |
| 5 | 默认开关 | **默认开**，`hunt_rounds=3`；`--hunt_rounds 0` 关闭 | 用户要求产品默认具备该能力 |

> 默认开的影响：每次 benchmark 跑会多跑 hunt 阶段（多耗 time/token）。因双轨隔离，
> **不改动任何分数**，只多产 `BUGS.md`。逃生口：`--hunt_rounds 0`。

## 3. 架构

### 3.1 流水线接线

```
stage_sequence = [
    server_deploy,
    checklist_generation,
    defect_detection,      # 不变：checklist 驱动，产 result.md → scoring
    defect_reverify,
    extract_result_file,   # 不变：产 result_extracted.md（打分用，轨一）
    defect_hunt,           # 新增：自由对抗狩猎，产 BUGS.md（轨二，不进 scoring）
]
```

- **插在最后**：`kill_local_server` 在 `run()` 的 `finally`（`claude_code.py:92`）里，所以 hunt
  运行时 server 仍活着，Playwright 可用；排在 `extract_result_file` 之后，**永不阻塞被打分的产物**。
- **失败语义（best-effort）**：`defect_hunt` 永远返回 `True`，不让它把 `run()` 的 `success` 标成
  error。注意这只是"不破坏 pipeline 成功标志"——若 hunt 内部出错，`_emit_event(status="error")`
  仍会在 `session_meta.json` 写下 `stage_success.defect_hunt=False`（`base_agent.py:284-287`），
  这是**有意保留的可观测痕迹**，不是污染。

### 3.2 开关透传

照现有 `reverify` / `require_evidence` 的 `kwargs.get` 套路：

- `eval/run_agent.py`：新增 `parser.add_argument("--hunt_rounds", type=int, default=3, ...)`。
  真正承载行为的构造点是 **`run_agent.py:127`**（调 `.run()` 的那个，已传 `record=record`）；
  `run_agent.py:97` 的 probe agent 只用于 `result_extracted_path.exists()` 检查、不跑 pipeline。
  两处都加 `hunt_rounds=args.hunt_rounds`（probe 加了也无害，`__init__` 的 kwargs 会吸收）。
- `eval/agent/claude_code.py` `__init__`：`self.hunt_rounds = int(kwargs.get("hunt_rounds", 3))`。
- `defect_hunt` 阶段开头：`if self.hunt_rounds <= 0: return True`（跳过，视作成功）。

### 3.2.1 【关键】修复早返回门，否则默认开的 hunt 在已打分数据集上永不执行

`run()` 与 `_run_record` 各有一处早返回，都吃 `result_extracted_path.exists()`：

- `claude_code.py:68`：`if self._should_skip_stage(self.result_extracted_path, "eval"): return True`
- `run_agent.py:107`：`if probe_agent.result_extracted_path.exists(): return`（**在构造真 agent 前就 return**）

因为 `extract_result_file` 排在 `defect_hunt` 之前，一旦打分产物存在，这两道门会让**整条
pipeline 短路**，hunt 永远跑不到——直接废掉"默认开 / 增量"的价值（在已跑过分的数据集上加 hunt
是最常见用法）。**必须同时改两处**，让"hunt 开启且 BUGS.md 缺失"时不短路：

```python
# claude_code.py run() 顶部
done = self._should_skip_stage(self.result_extracted_path, stage="eval")
hunt_pending = self.hunt_rounds > 0 and not self.bugs_path.exists()
if done and not hunt_pending:
    return True

# run_agent.py probe 门（同样条件）
if probe_agent.result_extracted_path.exists() and not (
    args.hunt_rounds > 0 and not probe_agent.bugs_path.exists()
):
    return
```

注意：短路解除后，`defect_detection`/`extract_result_file` 等阶段靠各自的
`_should_skip_stage(<自己的输出>)` 跳过已完成工作，只有缺 BUGS.md 的 `defect_hunt` 真正执行。

### 3.3 复用浏览器 agent 配置

hunt 复用 `_get_browser_agent_options(max_turns=self.max_turns)`：Playwright + 允许
`Bash`/`Read`/`Grep` 读源码与 seed，**禁 `Write`/`Edit`** —— 与 skill 铁律"绝不改代码"天然对齐。

### 3.4 【关键】产物写入路径：harness 写，不是 agent 写

**重要纠正**：agent **不能**自己写 BUGS.md——`_get_browser_agent_options` 把 `Write`/`Edit` 列入
`disallowed_tools`（`claude_code.py:584-587`）。现有 `defect_detection` 的真实机制是：harness 从
`ResultMessage.result` 拿到 agent **最终文本**，经 `_extract_final_result` 抽取后由
`self.write_markdown(target_file, final_result)` 写盘（`claude_code.py:165-178`）。**hunt 必须照抄
这套**，绝不靠 agent 写文件（这也顺带保住了 skill 铁律"绝不改代码"——Write 始终禁用）。

由此带来两个连锁约束：
1. **prompt 输出整份报告为最终文本**，顶部用**独立可解析的 header `# Bug Report`**（不可用
   `# Test Result`，否则与 `extract_result_file` 的 `# Test Result` 抽取逻辑串台，见 `base_agent.py:107-136`）。
2. **放弃 skill 的"发现即追加、不要批量"规则**（SKILL.md:218）——那套靠 agent 反复写文件，与
   "harness 抓一次最终文本"模型不兼容。改为：agent 在内存里累积，**最后一次性吐出完整 `# Bug Report`**。

`defect_hunt(self)` 主体（仿 `defect_detection`）：
```python
async for message in query(prompt=prompt, options=options):
    ... # _handle_message；捕获 result_message / num_turns
if num_turns > self.max_turns:
    self.write_markdown(self.bugs_path, "")          # 触预算：写空，best-effort
else:
    final, from_rm = self._extract_final_result(result_message, stage="defect_hunt")
    self.write_markdown(self.bugs_path, final)
return True   # 永远 True（§3.1 best-effort）
```
并初始化 `result_message=""`、`num_turns=0`（仿 `defect_reverify` L278-279），防 0-ResultMessage 时 `NameError`。

### 3.4.1 幂等与校验

- 输出：`<output_root>/<version>/<record_id>/BUGS.md`（顶部带覆盖率基准+快照）。
- 在 `BaseAgent` 加 `self.bugs_path = self.output_dir / "BUGS.md"` 与 `_has_required_bugs(text)`
  （识别 `# Bug Report` + 至少一个 `BUG-NNN` 块）。
- 幂等/续跑：`defect_hunt` 开头 `if self._should_skip_stage(self.bugs_path, stage="defect_hunt"): return True`。
- `_extract_final_result` 的 `check_final_fun` 映射（`claude_code.py:432-436`）加
  `"defect_hunt": self._has_required_bugs`。

## 4. Prompt 移植 — `eval/prompt/defect_hunt.py`

新建 `Template`，占位符 `$instruction` / `$server_url` / `$hunt_rounds` / **`$project_dir`**。**保真搬运 SKILL.md**：

> **【关键】必须传 `$project_dir`（被测项目源码的绝对路径）**：agent 的 `cwd` 是
> `./claude_code_cwd`（`claude_code.py:62,592`），**不是**被测项目目录。detection 能蒙混过关是因为
> checklist 已编码了行为；但白盒 hunt 的 Phase 1 要 `find . -type f` 读全部源码，若不显式告诉它
> 项目路径，会在错误目录扫描、**一无所获**。`local_project_dir` 已传进 `__init__`，把它的绝对路径
> 填进 prompt，agent 用 `Bash`/`Read`/`Grep` 按该路径读源码与 seed。

- **铁律**（抄现有 detection prompt L11 措辞，已验证）：不改代码、不修复、不建议修复、
  **绝不读任何 gold/reference/answer/expected-bugs 文件**，只凭运行时观测 + 源码判断。
- **Phase 1 白盒侦察**：用 `Bash`/`Read`/`Grep` 读全部源码 + seed/默认数据，建函数/分支/边界/
  输入入口/状态机清单 + 攻击面图。
- **Phase 2 攻击面分级** P0–P3（用户输入/认证 → 状态转换/持久化 → 错误处理/并发 → 配置环境）。
- **Phase 3 八类攻击向量**（按 web 场景精简）：边界值、状态机、缺失值、错误路径、并发、
  大数据、注入、项目 hygiene。
  - **不点名任何具体 bug 类**：prompt 只给通用攻击向量框架，要求 agent 从所读源码 + seed/默认
    数据**自行推导**该应用的攻击锚点（例如对比运行时环境与种子数据的各类假设），避免 prompt
    过拟合到某一类已知缺陷而压低对新缺陷类的召回。
- **DOM-only**：用 `browser_snapshot` 看可达性快照，禁截图（与现有 detection 一致）。
- **最终输出**：以 `# Bug Report` 为顶层 header（不可用 `# Test Result`），其下沿用 skill 的
  `BUG-{NNN}` 模板（严重级/类型/复现步骤/精确输入/期望/实际/代码位置:行号/攻击向量），header 下方
  紧跟覆盖率基准 + 每轮快照。**一次性吐完整报告**（不分批写文件，见 §3.4）。

注册：`eval/prompt/__init__.py` 加 `PROMPT_DEFECT_HUNT` 到 imports / `USER_PROMPT["defect_hunt"]` / `__all__`。

## 5. 有限多轮循环（C1：prompt 侧自管）

不在 Python 侧编排多次调用。单次 `agent.run()` 内，prompt 指示 agent：

- 最多跑 `$hunt_rounds`（默认 3）轮；
- 每轮结束更新 `BUGS.md` 顶部覆盖率快照；
- **早停判据**（任一满足即停）：
  1. 连续一轮没有发现新的 High/Critical bug；
  2. 触及 `max_turns` 预算。
- 每轮焦点参考 skill Phase 6：第1轮正常流程+P0 边界 → 第2轮状态机+缺失值 → 第3轮并发+错误路径
  +项目 hygiene。

预算硬封顶：`max_turns=150`（沿用现值），agent 自行在轮次间分配。

## 6. 改动文件清单

1. `eval/prompt/defect_hunt.py`（新）— `PROMPT_DEFECT_HUNT` 模板（含 `$project_dir`，输出 `# Bug Report`）
2. `eval/prompt/__init__.py` — 注册到 `USER_PROMPT` / imports / `__all__`
3. `eval/agent/base_agent.py` — `self.bugs_path` + `_has_required_bugs(text)`
4. `eval/agent/claude_code.py` — `__init__` 读 `hunt_rounds`；**改 `run()` 顶部早返回门（§3.2.1）**；
   `stage_sequence` 加 `defect_hunt`；新增 `async def defect_hunt(self)`（harness 写盘，§3.4）；
   `check_final_fun` 加 `defect_hunt` 校验
5. `eval/run_agent.py` — `--hunt_rounds`（`type=int, default=3`）+ 两处构造透传 +
   **改 probe 早返回门（`run_agent.py:107`，§3.2.1）**
6. `scripts/run_webtester_cc.sh`（可选）— 注释说明新 flag 与 `--hunt_rounds 0` 逃生口

## 7. 测试策略

- **单元**：`_has_required_bugs` 识别合法/非法 `BUGS.md`（有/无 `BUG-NNN` 块）。
- **prompt 占位符**：`PROMPT_DEFECT_HUNT.substitute(...)` 三个占位符都能填充、无 `KeyError`。
- **skip/幂等**：BUGS.md 已存在时 `defect_hunt` 跳过；`hunt_rounds=0` 时跳过。
- **冒烟**：在 1 条已知含缺陷的记录（如 0006）上 `--hunt_rounds 1` 实跑，确认产出非空、
  含可复现 bug 的 `BUGS.md`，且 `result_extracted.md`/score **不变**（双轨隔离验证）。

## 8. 非目标（YAGNI）

- C2 Python 编排多轮 + 跨轮去重（留 future，真需要时再上）。
- hunt 结果反哺 scoring / 映射到 TEST-ID（被"双轨并行"决策明确排除）。
- 真浏览器 visual / responsive / Core Web Vitals（skill 自身也把这列为弱项，需另配工具）。
