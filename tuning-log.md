# WebTestBench 调优日志 (tuning-log)

记录对 WebTester baseline 的诊断与逐步调优。每条改动都带：动机 → 改了什么 → 如何验证 → 结果 → 结论。

---

## 背景：问题与瓶颈定位

**症状**：当前 agent 对被测应用"基本找不到 bug"，recall/F1 偏低。

**Pipeline**：`server_deploy → checklist_generation → defect_detection → extract_result_file`。
评分是 **bug 导向**混淆矩阵：gold `pass=False` = 真 bug；TP = gold-bug ∧ 预测-fail；漏报(FN) = gold-bug ∧ (预测-pass 或 **未覆盖**)。

### 诊断 b — 清单覆盖率分类拆解 (2026-06-02)

`checklist_generation` 是**纯 intent**阶段（chat-only，`allowed_tools=[]`，只看 instruction，看不到应用/源码）。
用 `scripts/coverage_probe.py`（每类 2 条共 14 条，模型 sonnet，匹配复刻 `PROMPT_MATCH_ITEM`）只测"清单是否提到了 gold 项"：

| 类别 | gold | 覆盖率 | gold-bug | bug 覆盖率 | 漏掉的 bug |
|------|:---:|:---:|:---:|:---:|:---:|
| functionality | 126 | 75.4% | 37 | 73.0% | 10 |
| **constraint** | 54 | **29.6%** | 20 | **40.0%** | **12** |
| **interaction** | 36 | **44.4%** | 14 | 42.9% | 8 |
| content | 35 | 48.6% | 4 | 50.0% | 2 |

**头条**：75 个真实 bug 里 **32 个 (42.7%) 在清单阶段就不可达** = 铁定漏报。
- 漏掉的 CS bug 多是**领域专属业务规则**（过去/未来日期、start<end、唯一性、角色/状态权限）——抽象指令推不出。
- **第二个独立瓶颈**：record 0013 的 FT bug 清单覆盖了 (87.5%)，却被 `defect_detection` 标成 PASS (recall 0) → **检测阶段也坏**，与覆盖问题正交。

**结论**：覆盖是必要非充分门槛。F1 ≈ 覆盖率 × 检测转化率。两个瓶颈都要治。

### 原则确认（用户）
> 不存在"作弊"。用尽一切手段找 bug，**唯一硬约束：不读 ground-truth（gold checklist 答案）**。读源码/看页面/查 seed 数据均允许。

---

## 指标验证 (Step 1, 2026-06-02) — 先验地基，确保评估实现无误

工作流纪律（用户要求）：**每步先评估 → 验证指标实现无误 → 动手 → 消融对比（改动前后）**。

**做法**：写独立 oracle `scripts/verify_metrics.py`，**不 import scoring.py**，按 CLAUDE.md/docstring 定义重写 coverage + bug 导向混淆矩阵 + by_class，对 `claudecode-opus` 与 `claudecode-sonnet` 的 `WebTestBench_0013` **逐位断言** vs 已存 `score.json`。

**结果：PASS** — coverage / precision / recall / f1 / by_class 全部精确吻合（opus: conf(tp,fp,fn,tn)=(1,2,4,7) → P/R/F1=0.333/0.2/0.25；sonnet: (0,1,5,8) → 0/0/0）。→ **scoring.py 核心指标实现可信，可作为后续消融的金标准。**

**审计发现（非致命，记录在案）**：
1. **口径**：overall 与 by_class 的 P/R/F1 是**按记录 macro 平均**（每条算完再求均值），非 micro/pooled；by_class 的 coverage 在"含该类的记录"上平均，而 P/R/F1 在"该类含 bug 的记录"上平均（同一 by_class 块内分母不同）。**属设计选择，非 bug**，但解读时需知。
2. **precision 边界**：tp+fp=0（没有任何 fail 预测）时 precision 记 0.0 而非 None，略有惩罚性；但此时 recall=0 → f1=0，**无实际影响**。
3. **健壮性（scoring.py 待改）**：`_compute_metrics` 的 `pred_pass = all(pred_items[pred_id]...)` 若 LLM 匹配器返回了不存在的 pred_id（幻觉）会 **KeyError 崩溃**，无防护。建议加 `.get` 守卫。**未改**（属用户 eval 代码，待授权）。
4. **探针保真**：`coverage_probe.py` 的 coverage 定义与 scoring 一致 ✓；已修其正则使其和 scoring 一样识别 `**bold**` ID（避免漏算预测项）。**注意**：已跑的 P1 探针 A/B 两臂用的是同一个（旧）解析器，相对 delta 仍有效。

**关键结论（影响后续步骤）**：`coverage_probe` 测的是**覆盖率（recall 天花板）**，把所有项当 pass。它的"FP 面"gate（G1: Δbug_cov vs Δok_cov）是**启发式代理，不是真 precision**——覆盖一个 ok 项并不等于 FP（FP 需检测阶段把它标 FAIL）。**真正的 recall/precision/F1 必须跑完整 pipeline + scoring.py。** 因此 P1 至今只证明了"天花板抬升"，其**真实 F1 影响仍未测**。

---

## P1 真实指标消融 (Step 2, 进行中) — 用 scoring.py 金标准测真实 R/P/F1

**目的**：覆盖探针只测"天花板"。本步用**完整 pipeline（含检测）+ scoring.py（已验证无误）** 测 P1 对真实 recall/precision/F1 的影响。

**配置（用户定）**：sonnet 跑检测（CLI 凭证）；scoring 匹配用 MiniMax-M3（`https://api.minimaxi.com/v1/chat/completions`，端点已验证 200 可用）。样本 5 条，挑自"P1 新覆盖了 bug"的记录，跨 5 类：
`0002`(Commerce,CS) `0005`(Tool,IX) `0006`(Data Mgmt,CS+IX) `0007`(UGC,CS) `0024`(Presentation,IX+CT)。

**去耦设计**：两臂的 `checklist.md` 用 `scripts/gen_checklists.py` 各自预生成（OLD prompt→`p1abl-old`，NEW prompt→`p1abl-new`；OLD 16-20 项，NEW 21-25 项），检测阶段因 `_should_skip_stage` 跳过清单生成、只跑检测，故**检测不依赖 prompt 文件**、两臂可独立跑。编排见 `scripts/run_p1abl.sh`。

**可行性已验证**：冒烟（old/0002）端到端跑通——server 部署 OK、预置清单被采用、检测真跑 Playwright+Bash（旁注：检测阶段**有 Bash 权限**，会读源码+脚本化 Playwright，非纯 DOM——对 P2 有用）。

**成本现实**：检测受 Claude CLI 限速影响，单条 >25 分钟未完；9 条顺序跑预计数小时。

### 结果 (2026-06-02, sonnet 检测, MiniMax-M3 匹配)

样本砍到 3 条提速（复用 old 臂已完成的 0002/0005/0006）。**0006 作废**：检测正常完成，但 agent 输出用了 `### FT-01 **PASS**` 而非 prompt 要求的 `- [X] FT-01:` 复选框格式 → scoring `_parse_pred_checklist` 解析 0 项 → 静默回退 checklist.md（全 pass）→ 两臂 recall 0，无信号。**干净记录：0002、0005。**

| 记录 | 臂 | coverage | precision | recall | F1 |
|---|---|:---:|:---:|:---:|:---:|
| 0002 (Commerce/CS) | old | 0.63 | 0 | 0 | **0.0** |
| 0002 | new | 0.84 | 0.75 | 0.60 | **0.667** |
| 0005 (Tool/IX) | old | 0.56 | 1.0 | 0.33 | **0.5** |
| 0005 | new | 0.61 | 1.0 | 0.33 | **0.5** |

- **0002 = P1 覆盖→F1 转化铁证**：old 标 6 个 FAIL 却 tp=0（误报、漏掉真 bug）；new 覆盖并检出 CS"过去日期"bug（CS P/R/F1=0.67/1.0/0.8）→ F1 0→0.667。
- **0005 = 净持平**：new 赚 CS bug 但丢 FT bug（old FT=(1,.5,.67)→new FT=0）→ 印证审查"挤占 FT"担忧。
- **2 条均值 F1：old 0.25 → new 0.58（≈翻倍），但 n=2 极薄，仅方向性。**

### 结论
P1 的覆盖增益**能**转化成真实 F1（0002 实证），但**有代价**（0005 挤占 FT）且样本太小。**方向性支持 P1，但不足以定论。**

### ⚠️ 上表已被指标 bug 污染——修复后见下方"修正结果"

### 关键发现：指标完整性 bug（须先修）
`scoring._parse_pred_checklist` 只认 `- [x] ID:` 复选框格式；当 detection 输出跑偏（如 `### FT-01 **PASS**`），解析 0 项 → **静默回退 checklist.md 全 pass → recall 0**。后果：**把"格式跑偏"误判为"检测失败"**，可能污染历史结果中部分 recall=0 记录（间歇性，取决于 agent 当次输出格式）。
- **修复方向**：(1) scoring 解析增强，兼容 `### ID ... **PASS/FAIL**` 等变体；(2) 当 result_extracted 存在但解析 0 项时**显式标记为 parse_error，不静默当全 pass**（避免伪装成检测漏报）；(3) 或在 detection prompt 端强约束输出格式 + 加后处理校验。
- 这是"先验地基"原则的延伸：**在用 F1 下结论前，先堵住这个静默污染源。**

---

## 指标修复 (2026-06-02) — 堵住静默回退污染

**改动（`eval/scoring.py`，已通过 `verify_metrics.py` 复验未破坏原路径）**：
1. **解析器增强** `_parse_pred_checklist`/`_parse_pred_items`：复选框 `- [x] ID:` 优先；解析为 0 时回退识别 `### <ID> …` + 其后首个 `**PASS/FAIL**`/`**Status: PASS**` 标记。实测捞回 0006：old 20 项(18P/2F)、new 25 项(17P/8F)，与 result.md Summary 吻合。
2. **堵静默回退**：result_extracted 存在但解析 0 项时，标 `parse_error` 并**排除**（不再用 checklist 当全 pass），新增 `parse_error_ids` + `parse_errors.json` + score_avg `counts.parse_error`，并在 `no_missing` 切片排除。

**修复揭示了一个被掩盖的 P1 胜绩**：0006 修复前两臂都"recall 0"（伪装成检测失败），修复后真相是 **new 臂抓到一个 CS bug（CS=(1.0,0.5,0.67)，F1 0→0.222）**。

### P1 真实消融（修复后，n=3 干净，sonnet 检测 / MiniMax-M3 匹配）

| 记录 | old F1 | new F1 | 机制 |
|---|:---:|:---:|---|
| 0002 (Commerce/CS) | 0.0 | **0.667** | new 覆盖+检出"过去日期"CS bug；old recall 0（误报、漏真 bug） |
| 0005 (Tool/IX) | 0.5 | 0.5 | 持平：new 赚 CS bug 丢 FT bug |
| 0006 (Data Mgmt/CS) | 0.0 | **0.222** | new CS=(1.0,0.5,0.67) 抓到约束 bug |
| **均值 F1** | **0.167** | **0.463** | **≈2.8×** |

**结论（修正后）**：P1 的覆盖增益**确实转化为真实 F1**，增益集中在 **CS 类**（主攻方向命中）。代价：0005 挤占 FT；0002 new precision 0.75（1 个 FP，印证审查 precision 风险，但净 F1 强正）。**n=3 仍小，结论方向性强但需更大样本坐实。**

**下一步候选**：(a) 扩样本坐实 P1；(b) **P2 修检测**——0002-old"标 6 个 FAIL 却 recall 0"、0005 丢 FT，说明检测质量是另一大头（对抗性验证 + 输出格式强约束，后者还能根治本次的 parse 污染源头）。

---

## P2 检测阶段调优 (评估 + 独立 opus 审查 + P2-0, 2026-06-02)

### 评估：检测失败模式（item 级诊断, 记录 0002/0006）
- **covered-but-PASS FN**：清单提了、检测标 PASS 却漏真 bug（0002 id12"无电话也能预订"、0006 id19"Save 无反应"）→ 检测漏检。
- **FP**：好功能标 FAIL（0002-old 4 个）。**机制**：scoring `pred_pass = all(mapped_preds)`，多个 pred 命中同一 gold 时一个 FAIL 即翻转 → gold-ok 成 FP。P1 清单变长会**放大**此粒度型 FP。
- **🚨 工具混淆（代码级证实）**：detection 设 `allowed_tools=PlaywrightTools` 但 **Bash 仍可用**——SDK 里 `allowed_tools` 只是"自动批准列表"非可用性门禁，真正门禁是未设的 `tools`（默认全部内置可用）。冷启 smoke(old/0002) 用 27 次 Bash/0 次 MCP（MCP 没连上→回退 Bash），其余跑用 MCP。**后果**：工具 profile 不一致；**0002 的 P1 消融臂被混淆（old=Bash/new=MCP），0005/0006 两臂同 MCP 干净**——P1 结论（0006 净胜/0005 持平）仍立，0002 头条打折。

### 独立 opus 审查（代码级verified）→ 判 RETHINK，修正顺序/范围
- BLOCKER：基线被工具混淆，**必须先修工具→重建干净基线→再归因**；n=3 单跑统计功效不足（F1 摆动 0.2-0.7/翻转），需扩样本+重复+报方差。
- A=唯一直击 covered-but-PASS 的杠杆但最易制造 FP（净符号未知，需隔离+precision gate）；C 与已落地的健壮解析器冗余（别记 F1 功劳）；B 基本 prompt-theater（低成本留着）；**D 删除**（针对 scoring artifact 且与 P1 对冲）。
- P2-0 选"一致允许白盒"：删 prompt 矛盾行 + 修 MCP 可靠性。

### 决策（用户）：①一致允许白盒 ②中道扩+重复 ③删D+立 all() 指标 RFC

### P2-0 已实现（工具一致性）
- `eval/prompt/defect_detection.py`：删"禁用 Bash/Read/Write"矛盾行；明确 Playwright 优先 + 可用 Bash/Read 读源码/seed；**禁读任何 gold/reference/answer 文件**；禁 Write/Edit 改应用。
- `eval/agent/claude_code.py` `_get_browser_agent_options`：`allowed_tools` 显式加 Bash/Read/Grep/Glob（sanction 白盒）；`disallowed_tools` 加 Write/Edit（防改应用）+ 保留禁截图。
- MCP 可靠性：包已缓存、冷启失败本会话不复发；两路均 sanction 故回退不再是能力混淆。**fresh 环境建议 prewarm**（npx 预热）——待需要时加。

### P2 Bundle 消融 — 隔夜运行部分失败 (2026-06-03)
首轮 `run_p2abl.sh`（24 跑）撞**外部限速/退化**：base-r1 全 6 条成功后，base-r2 第 4 条起 + 整个 P2 臂均报 `Claude Code returned an error result`（run_agent.py:129 首错即 `sys.exit(1)`，但 P2 臂每条一上来就错）。**非 setup bug**（base-r1 完整证明 harness/prompt/清单跳过/MCP 都正常）。
- 可用：**base-r1 6/6 ✓**；base-r2 3/6；p2 臂 0/6。
- CLI 跨夜已恢复。**补救**：`run_p2_redo.sh` 按记录隔离+重试重跑 p2-r1（6 条），得 base-r1 vs p2-r1 的 1-repeat 干净对比（在已批 $48 预算内）。2nd repeat 视信号再补。
- **教训**：长无人值守跑要按记录隔离（run_agent 首错即终止不适合）；单臂别一次性 6+ 条堆给限速。

### P2 Bundle 消融结果 + 诊断 (2026-06-03, n=6, 1-repeat: base-r1 vs p2-r1)
均值 P/R/F1：BASE 0.389/0.151/0.212 → P2 0.394/0.207/0.248。表面 recall+37%相对、precision 不塌、parse_error 两臂均 0（C 生效）。

**但诊断揭穿了"增益"**：
- item 级：P2 只比 BASE 多抓 **1 个 bug**（0001 id4，是 **FT 不是 CS**）；其余 5 条两臂抓到的 bug **完全相同**。→ 增益是噪声，非 A 之功。
- 日志 grep：**BASE 检测 agent 本就在做对抗尝试**（empty 19/duplicate 8/past date 6/negative 6/invalid 5…），P2 仅略多。→ **A（对抗约束验证）冗余**，agent 默认即会试非法值。
- covered-but-PASS 的 CS bug 两臂都漏（0002 id12、0006 id19）：不是"没试"，是**agent 试了却误判是否被拦**。**瓶颈=检测判断准确度，prompt 治不了。**

**定论**：**检测 prompt 微调（A/B）杠杆极低**（agent 已具备该行为）。保留 **P2-0（工具一致性）+ C（格式根治 parse 污染）** 作 correctness 改进；A/B 不涨 F1。真正的检测提升需换杠杆（如对 covered-but-PASS 项做二次复核、或换判断机制），非本轮 prompt 能解决。

## RFC: scoring `all()` 语义 vs 多数投票 (2026-06-03) — 结论：保留 all()

**背景**：`_compute_metrics` 判定一个 gold 项"被预测为有 bug"用 `pred_pass = all(mapped_preds)`（any-fail-wins）。担忧：多个 pred 命中同一 gold 时，一个误判 FAIL 即把 gold-ok 翻成 FP（粒度型误报）。

**数据**（扫 19 条已打分记录，跨 p1abl/p2abl/claudecode 版本）：
- gold-OK 多映射(>1 pred)：45；gold-BUG 多映射：17。
- **FP 总 27，多数投票能洗白的仅 4（15%）**；其余 23 是真实过度标记。
- TP 总 20，多数投票会误杀 2。

**结论：保留 `all()`，不改多数投票**。理由：
1. 粒度型 FP 占比小（4/27），多数 FP 是真实检测过度标记——FP 的真正杠杆在检测判断力，非 scoring 语义。
2. `all()` 对 recall **宽松**（一个 pred 命中即 TP）；当前主矛盾是 recall 低，换多数投票净亏 2 TP，**让最弱处更弱，方向错**。
3. 改语义会破坏与历史结果可比性。
**行动**：仅记录粒度 caveat；如需可把多数投票作为**附加**报告指标，不作主指标。

### 待办（审查推荐顺序）
重建干净基线(带重复/方差) → 隔离消融 C→A(precision gate)→B → held-out 记录 → 另立 `all()`→多数投票 指标 RFC → 对齐轮数预算(prompt 100 vs max_turns 150)。**下一步成本巨大，需先定预算。**

---

## 调优 P1 — 强化清单生成 prompt（纯 intent，无接地气）

**动机**：CS/IX 覆盖最差，而**大部分 CS 约束其实可从指令推导**（"婚礼日期不能是过去"不用看应用就知道该测）。先用最便宜、零基建、**无白盒陷阱**的招攻 CS/IX 覆盖。

**为什么不先做完整 `app_exploration` 接地气阶段**：两轮独立 opus 审查指出——
1. **白盒陷阱 (BLOCKER)**：读源码*生成*清单会压制掉"缺失校验"型 CS bug（代码里没这个守卫 → 清单也不写），而那正是要抓的。
2. 检测阶段并列瓶颈，只补覆盖 F1 可能不动。
3. precision 反噬 + 成本。
→ 降级为先做 prompt-only 的 P1。

**改了什么**（`eval/prompt/checklist_generation.py`，旧版快照 `/tmp/checklist_generation_OLD.py`）：
1. CS 段加**强制 intent 枚举**指令 + **illustrative（非封闭）约束类型清单**：时间合法性 / 唯一性 / 必填空值 / 角色权限 / 状态机 / 数值范围 / 冲突双占。"即使不确定应用是否实现也要写——测试会揭示缺失。"
2. IX 段加**预期动态反馈枚举**：toast/弹窗/跳转/计数更新/状态刷新/启用禁用/媒体播放。
3. 加 **4 个跨域通用示例**（过去日期、角色权限、锁定态、删除确认），防 commerce 锚定；明确"不照抄、改 *kind*"，且不取自 14 条测试样本。
4. Rule 7：上限 20 → **25（加性）**，明确**不夺 FT 名额**，把额外容量给 CS/IX。

**第二轮 opus 审查采纳的护栏**：
- 上限**加性**提升（不挪 FT）。
- taxonomy 写成 illustrative 防过拟合 14 条。
- A/B 加 precision proxy gate + FT 不回退 gate。
- 14 条 delta 当**乐观上界**，非泛化估计；真正 F1 解锁靠后续修检测。

**如何验证**（增强 `scripts/coverage_probe.py`，加 `--out`/`--baseline` + 三道 gate）：
- 同 14 条、同模型 (sonnet)、同匹配器跑 old-prompt vs new-prompt，相对 delta 可信。
- **G1 FP-control**：Δbug_cov ≥ Δok_cov（增益要集中在 bug，别只是扩大 FP 面）。
- **G2 FT 不回退**：FT 覆盖率不下降。
- **G3 读数**：净新增 bug 覆盖项 vs 净新增 ok 覆盖项（FP 面）。

### 结果 (2026-06-02, sonnet, n=14, 同 14 条/同匹配器)

基线臂用旧 prompt 经增强探针重跑（`summary_old.json`），实验臂新 prompt（`summary_new.json`）。

| 类别 | 覆盖率 Δ | **bug 覆盖率 Δ** | 漏掉 bug | ok 覆盖率 Δ(FP面) |
|------|:---:|:---:|:---:|:---:|
| functionality | 71.4→77.8 (+6.4) | 67.6→70.3 (+2.7) | 12→**11** | 73.0→80.9 (+7.9) |
| **constraint** | 33.3→48.1 (+14.8) | 40.0→**60.0 (+20.0)** | 12→**8** | 29.4→41.2 (+11.8) |
| **interaction** | 44.4→58.3 (+13.9) | 35.7→**71.4 (+35.7)** | 9→**4** | 50.0→50.0 (0) |
| content | 54.3→42.9 (**−11.4**) | 50.0→50.0 (0) | 2→2 | 54.8→41.9 (−12.9) |

**头条**：总 bug 覆盖率 **53.3% → 66.7%**；铁定漏报 bug **35 → 25**（−10，约 −29%）。

**三道 gate**：
- ✅ **G1 FP-control**：Δbug_cov(+13.4) ≥ Δok_cov(+4.0) — 增益集中在 bug，不是在扩 FP 面。
- ✅ **G2 FT 不回退**：71.4% → 77.8%。
- **G3 读数**：净新增覆盖 **bug +10**，ok(FP面) +7 — bug 增益 > FP 面增益。
- **OVERALL：PASS**。

### 结论

P1 在**覆盖维度成功**：CS/IX 这两个最差的类 bug 覆盖大幅抬升（CS +20、IX +35.7），漏报缺口收窄近三成，且未牺牲 FT、增益是 bug 导向的。验证了"大部分 CS/IX 约束可从 intent 推导"。

**诚实的 caveat（须记住）**：
1. **Content 覆盖回退 −11.4**：配额向 CS/IX 倾斜挤压了 CT。但 CT bug 覆盖不变（50%）、漏报 bug 仍是 2，**对找 bug 无害**，只是 ok 项覆盖降了。可接受；若要救 CT 再微调。
2. **phantom 预测暴涨**：新 prompt 每条出 23–25 项（旧 17–21），`phantom_pred`（匹配不到任何 gold 的项）从 ~1–9 涨到 ~6–13。这些项与 gold 无关，**不直接进 bug 导向 precision 矩阵（该矩阵以 gold 为中心）**，但会**白耗检测阶段轮数**、增加噪声。属待观察项。
3. **这是乐观上界**：sonnet、n=14、taxonomy 部分反推自这 14 条；泛化需held-out 验证。
4. **真正的 F1 解锁仍待 P2 修检测**：0013 证明覆盖到的 bug 也会被检测标 PASS。P1 只抬了天花板。

**下一步建议**：
- (a) 跑**真 pipeline + scoring.py**（含 detection）在抽样上确认 recall/precision/F1 实际位移（需外部 API key 或 CLI 复刻 scoring 指标）。
- (b) **P2 修检测阶段**（对抗性验证：主动输异常值并断言被拒）——预计这才是兑现 F1 的关键。
- (c) 视情况微调：救回 CT 覆盖、抑制 phantom（如对"指令未提及的功能不要臆造测试项"加一句）。

---

## P3: 检测二次复核（defect_reverify）—— 实测结论：无害但未证明有效，受上游格式所限 (2026-06-03)

**做了什么**：新增 gated `defect_reverify` 阶段（默认关，`--reverify`）。第一遍标 PASS 的项，在全新盲判 browser 会话下用**证伪 prompt**重测，**证据门控 union-of-failures** 合并（复核 FAIL 且带 Bug Report 才翻）。纯逻辑在 `eval/agent/reverify_reconcile.py`（10 单测）。设计/计划见 `docs/superpowers/specs|plans/2026-06-03-detection-reverify*`。独立 opus 设计审查 + 整体审查均过（REWORK→修五条致命后 READY）。

**实测（baseline 源 = p2abl-base-r1，minimax 匹配，sonnet 复核）**：
- **0002（handoff 点名的 CS 漏判 id12，格式干净）**：
  - 全类无门控（smoke1）：翻转 FT-02 + IX-04，**俩都是 FP**（FT-02 是"种子数据全 2025 日期、2026 跑全成过去"的**时间漂移 FP**）。overall P 0.5→0.333、F1 0.286→0.25。**净负，精确命中审查官致命问题②预言。**
  - 加补丁后（smoke2，翻转限 CS/IX + prompt 加"忽略时间漂移/保守判 FAIL"护栏）：只剩 IX-04 翻转（uncovered，中性）。overall 逐项 == baseline（P0.5/R0.2/F1 0.286）。**FP 害处消除 → 净中性。**
  - **但 constraint 三轮全程 0.5/0.5/0.5**：那个目标 CS 漏判，**复核重测了仍判 PASS，没救回**。"同模型再判救不了系统性误判"。
- **0001 / 0006（3 记录 A/B）**：**复核根本没真跑**。其 detection 输出是**标题格式**（`### FT-01 ... PASS` / 内联 `**IX-04: PASS**`），`parse_pass_items`（只认 canonical checkbox）解析到 0 PASS → 旧版**静默 no-op**。聚合 P/R/F1 两臂完全相同（0.167/0.067/0.095），仅 coverage 0.43→0.59。
- **评估发现（关键）**：连 **scoring 自己的 header 回退都误判 0006**——把 `### BUG-01 · CS-02` 这种 bug 报告小节标题当成 item（抽出 5 个幻影 BUG-xx id）。**0006 的 detection 输出乱到上游解析就已污染**；即使 P2 臂（带 C 格式修复）0006 仍是 25 行标题、0 checkbox。

**定论**：
1. reverify 机制可用、默认关不污染 baseline、全降级路径安全；补丁后**无害**。
2. **但价值未兑现**：唯一能跑的 CS 记录（0002）没救回误判；最该救的 0006 被上游格式挡在门外。**lever 触达性被上游 detection 格式方差卡死。**
3. 已把静默 no-op 改为**响亮** `reverify_unparseable` 事件 + skip 标记（`has_canonical_items`），不再伪装"跑了没翻"。

**浮现的新 route（更高杠杆）**：**根治 detection 输出格式方差**——让 detection 在所有记录上可靠输出 canonical checkbox（0006 类顽固标题/内联粗体会污染 scoring 与一切下游）。这比继续砸钱 reverify 更值得投。

**reverify 复活的前提**（若日后要捡）：先修上游格式，再给 `parse_pass_items`+`reconcile` 加"先归一化为 canonical checkbox"前置步（用 scoring 的双格式解析器，但要先剔除 BUG-xx 幻影 id），才能在 0006 类记录上真跑。

---

## Step 1 — Output-format reliability (canonicalize) — SHIPPED & ABLATED (2026-06-04)

Implements the format route flagged above. New `eval/canonicalize.py` (`normalize_to_canonical`, `count_phantom_ids`) wired into `scoring._parse_pred_items` behind `--canonicalize` (default off); the header fallback also drops phantom `BUG-\d+` ids unconditionally (belt-and-suspenders). Branch `feat/selfhunt-format-and-evidence`. Plan: `docs/superpowers/plans/2026-06-04-selfhunt-format-and-evidence.md`.

**Gold-independent ablation** (baseline reverify-off detection artifacts copied into `_canon_off`/`_canon_on`, scored OFF vs ON; harness `scripts/ablate_canonicalize.py`):

| record | phantom(raw) | empty_match OFF | empty_match ON | num_pred | coverage OFF→ON |
|---|---|---|---|---|---|
| 0001 | 0 | False | False | 25 | (canonical, unaffected) |
| 0002 | 0 | False | False | 22 | idempotent, unaffected |
| **0006** | **5** | **True** | **False** | 25 | **0.0 → 0.524** |

**Verdict: KEEP.** Both gates pass: (1) phantom-id rate → 0 deterministically (0006: 5→0); (2) `empty_match` cleared on the heading-form record (0006 OFF empty_match=True/F1 0/cov 0 → ON scored, cov 0.524) — the matcher succeeds once it receives canonical, phantom-free, correctly-PASS/FAIL-labelled input. No regression on already-canonical records (0002 idempotent; 0001 unaffected). A real-data bug was caught during TDD: 0006 uses `**Result: PASS/FAIL**` status lines that the first `_DEDICATED_STATUS_RE` missed → all-pass; fixed and locked by a regression asserting 0006's 7 FAILs (FT-04/05/06, CS-02/03, IX-01, CT-02) survive normalization.

**Scope note:** ON 0006 F1 is still 0 — format is necessary but not sufficient; the product's actual 0006 judgments are weak (self-hunt's canonical 0006 scored F1 0.25). Improving judgment is Step 2's job (evidence-forced). Step 1's win is that a format-crashed record is now *scored* instead of silently flagged `empty_match`.

---

## Step 2 — Evidence-forced judgment — CODE COMPLETE, ABLATION RUN PENDING (2026-06-04)

New `eval/agent/evidence_lint.py` (`find_unsupported_pass`, flag-only) + `EVIDENCE_REQUIREMENT` block in `eval/prompt/defect_detection.py`, threaded behind `--require_evidence` (default off) through `run_agent.py` → `base_agent.py` → `claude_code.py` (mirrors `--reverify`). When on: appends the evidence requirement to the detection prompt and, after detection, emits an `__EVENT__ evidence_lint` listing PASS items with no `- Evidence:` line. Off = byte-equivalent no-op (verified). Unit tests: 8 evidence_lint + 13 canonicalize = 21 green; full suite 31 green. Harness: `scripts/ablate_evidence.py`.

**Status: code shipped & unit-tested; empirical ablation NOT yet run.** The Step 2 ablation requires two full WebTester agent runs (baseline vs `--require_evidence`) with a FIXED checklist, which needs the **agent model's API config** (the `XXX` placeholders in `scripts/run_webtester_cc.sh` for `API_BASE_URL`/`API_KEY`/`MODEL`) — the MiniMax key wired earlier is only the scoring matcher, not the agent. That config was not available in this session, so the run is deferred. Hard checkpoint honored for Step 1 (passed its gold-independent gate); Step 2's judgment gate (item-level mis-PASS→FAIL flips on 0002-class records, no new FPs, drift-free-subset F1 non-regression, tool-call covariates) is to be evaluated once the agent runs are produced.

**To run Step 2 ablation:** set the agent API in `scripts/run_webtester_cc.sh`; run the agent on the record set into `_evid_base` (no flag) and `_evid_on` (`--require_evidence`), copying the baseline `checklist.md` into the evidence run dirs before detection (confound control); score both; then `python scripts/ablate_evidence.py`.

---

## Step 2 — Evidence-forced judgment — ABLATED (2026-06-04): NO-OP (harmless, unproven)

Ran the real ablation on record **0002** (the designated judgment-miss target). Agent = **sonnet** (CLI creds); scoring matcher = MiniMax-M3 with `--canonicalize`. Confound control: baseline run produced `checklist.md`; it was copied into the evidence run dir so `checklist_generation` SKIPPED (verified) and only `defect_detection` differed by `--require_evidence`.

**Real-data format fix needed first:** the sonnet agent emitted `- [x] **FT-01** — desc` (bold id, em-dash, NO colon) — a dialect `normalize_to_canonical` didn't cover; scoring parsed 0 items. Extended canonicalize (`_CHECKBOX_LOOSE_RE`, commit bee5e71) → the 0002 baseline now parses to 24 items. (Another instance of real agent output exceeding the prompt's STRICT FORMAT.)

**Evidence requirement was followed:** `__EVENT__ evidence_lint` = "0 PASS item(s) lack Evidence" (21 Evidence lines emitted).

**Result (F1 re-scored 3× per arm to separate matcher variance from real effect; agent output is fixed, only the matcher is non-deterministic):**

| arm | F1 (3 matcher draws) | item-level PASS→FAIL flips | tool-calls |
|---|---|---|---|
| baseline | 0.500, 0.286, 0.286 | — | 83 |
| evidence | 0.286, 0.286, 0.286 | **0** (of 24 common items) | 73 |

**Verdict: NO-OP.** The two arms are statistically the same (mode F1 0.286; baseline's 0.500 was a single lucky matcher draw, its repeats == evidence). The evidence requirement changed **zero** judgments. Root cause: **a sonnet agent does not commit the rubber-stamp-PASS-without-observing mistake that Step 2 targets** — on 0002 it worked around the time-drift by creating its own future-dated events to populate the grid, then tested, rather than leaving the grid empty and marking browse PASS (which is exactly what the weaker product baseline did). So the target failure mode does not reproduce with a capable agent, and the structural evidence nudge has nothing to correct. Consistent with the prior P2 finding ("the agent already does adversarial QA; judgment is the limit and prompt-tuning can't move it") — now extended: a structurally-enforced evidence field is also ~no-op for a capable agent.

**Disposition: KEEP behind flag, default OFF; do NOT enable by default.** Harmless (lint clean, format fine, no budget blow-up, no FP introduced), but value unproven. To ever demonstrate value you'd need a record where a *capable* agent genuinely mis-judges a covered item — the time-drift records are not it (sonnet routes around them). n=1 (single record); broader runs are unlikely to overturn the structural conclusion and cost full agent campaigns. The reusable assets (evidence schema, `evidence_lint`, harness) remain for future weaker-agent or stricter-escalation (option b/c) experiments.

**Net for the branch:** Step 1 (canonicalize) is a real KEEP win (unblocks scoring on format-crashed records). Step 2 (evidence) is harmless-but-no-op on a capable agent — ship the code behind its default-off flag, do not turn it on.

**Default flipped (2026-06-04):** `canonicalize` is now **default-ON** in `scoring.py` (constructor default `True`, `parse_args` default `True`); added `--no-canonicalize` to disable for A/B repro. Rationale: once Step 1 passed its gate, leaving a proven-useful, harmless, idempotent normalization behind a default-off flag was inconsistent with how prior proven tuning shipped (P1/P2 keepers/parser fixes were all on-by-default; only the *unproven* P3 reverify stayed flagged-off). Caveat: re-scoring existing `outputs/` runs now normalizes by default, which rebases historical scores to the (more-correct) canonical parse — intended.

---

## 方向 B 全链：发现 → 验证 → 回写 gold → matcher 投票 (2026-06-09)

**先补 lineage（本日志在 06-04 后漏记的已合并改动；git/docs/specs 有据）**：
- **PR#4** `defect_hunt` 阶段（chaos-qa 白盒，读全源码+8 向量攻击，写 `BUGS.md`，best-effort、默认 `hunt_rounds=3`、不进打分）。
- **PR#5** **P1-A** 检测离清单对抗探索（`defect_detection.py §3`：判完 checklist 后用剩余预算找清单外 bug，输出 `EX-NN` Additional Findings，默认开）。
- **PR#6** **变异 catch-rate 尺**（`scripts/mutation_probe.py`：注入已知 bug → 真检测 → 3 票判定是否抓到；gold 独立、对 gold 不完整/漂移免疫；~45min/run）。

**本轮动机**：P1-A 在 mini 集挖出的 `EX-NN` 都匹配 0 gold——是真 bug 但 gold 看不见（gold 不完整封顶可测 recall）。走**方向 B：真去找更多 gold bug，限 7-app mini 集**，全程不读 gold、不靠变异尺。

**① 发现**：P1-A 在 5 个 app 各出 1 个 EX-01 离清单候选——0009 订房日期 UTC 偏移差一天 / 0035 消息内存存储刷新即丢的假成功 / 0037 年龄筛选桶错 / 0080 fuzzy 过宽返回全部 / 0089 空字段漏裸 Markdown。

**② 验证（gold-blind，ultracode 工作流）**：每候选 3 视角（机制确认 / 对抗反驳 / 用户可观察性）直接读真实源码、**禁读 gold**，拿 app 自带依赖（date-fns v3、micromark 4.0.2）/ 种子数据（errors.ts、pets.ts）**实跑复现**。**结论 4 铁案 + 1 大概率，0 反驳、0 存疑**：
- 0009/0037/0080/0089 **确认真**；0035 facts 三方无争议（messageStore 无持久化、`getMessagesForBooth` 死代码、800ms 假延迟 + "Message sent!" toast、无后端），但"算不算 bug"取决**产品意图**，记**大概率真**。
- 两处机制比 EX 更准：**0037** 根因是 `PetsPage.tsx:50` 按手写 `ageGroup` 字符串过滤（非边界数学）、种子标签自相矛盾（Luna 3→adult、Duke 7→senior）；**0080** 是 `useFuzzySearch.ts` 子序列匹配 + **无最低分阈值**。

**③ 回写 gold**：5 条验证 bug 结构化追加进各 app 的 `checklist`（gold schema `{id,content,class,pass:false,bug}`，新 id 0009→18 / 0035→16 / 0037→17 / 0080→19 / 0089→27）。⚠️ **数据集 gitignored，改动只在本地**，备份 `data/WebTestBench/WebTestBench.jsonl.pre-goldwriteback.bak`（其余 95 条原样未动）。

**④ 度量揭示新瓶颈 = matcher 噪声**：同一批 p1exp 检测输出，对旧/新 gold 对照重打分（必须清 `score_match_ids.json` 缓存，否则 `scoring.py:692-698` 无脑复用）。回写只在 LLM matcher 把检测的 `EX-01` 连到新 gold 项时才抬 recall。3 次单跑（K=1）落地集 {2,4,5}/5，**并集 5/5（全部可匹配、gold 文案没问题）**，但单跑随机欠数，把均值 recall 从 ~0.365 压到 0.273。**瓶颈已从检测/gold 完整性转移到保守随机的单次 LLM matcher（MiniMax-M3）。**

**⑤ matcher 投票（PR#7，已并入 main `1ae9d13`）**：`eval/scoring.py` 加 `--match_votes K`（默认 1 = 现状、非破坏）。纯函数 `aggregate_ballots` **union τ=1**：每 pred 取 K 票里非 None gold 的**众数**，平票取**最小 gold id**（数值感知 `'2'<'10'`），gold id **str 归一**（顺带修了潜在 int/str key 不匹配 bug，故 K=1 只升不降）。`_get_matches` 重构为 `_match_once ×K`、丢失败票（不否决）、全败→None、**votes-aware 缓存**。34 新测试，全套 **103 绿**。spec `docs/superpowers/specs/2026-06-09-matcher-voting-design.md`。经 brainstorm→spec→ultracode 工作流（对抗设计评审→TDD→对抗验证）。

**实证（5-app 新 gold，K=1 vs K=3）**：

| 配置 | 落地 | 均值 recall |
|---|---|---|
| 旧 gold 基线 | — | 0.261 |
| K=1（单次，噪声） | 2~5/5 | 0.273–0.365 |
| **K=3（投票）** | **5/5（一趟）** | **0.365（+0.104，相对 +40%）** |

**成本/坑（重要）**：MiniMax-M3 推理模型 **~60–80s/call** → K=3×5 records ≈ 20 分钟。`_call_api` 有 `timeout=120` 但 `retry=5`，API 抽风时整批可拖数小时（本轮踩过一次"看似挂 3 小时"，实为**慢 + API 抽风、非死锁**）。**大规模跑必须 shell `timeout` 兜底 + 控并发。**

**已知 low-sev（PR#7 未修）**：首跑"缺结果 + 空预测 + 无缓存"的罕见记录会白烧 K 次调用（结果对 `[]`、仅浪费）。

**caveat**：0009 时区相关（本机 UTC+8 成立，`TZ=UTC` 会掩盖）；0035 严重度取决产品意图。

**定论**：方向 B 全链跑通——检测**能**找到 gold 结构上看不见的真 bug；验证（gold-blind）+ 回写 + **投票去噪**后，这些 bug 可**可靠**转化为可测 recall（5-app 均值 0.261→0.365）。matcher 投票这把尺惠及**所有**未来打分，非仅这 5 个 app。**下一步候选**：(a) 把"验证→回写"推广到 `_eval_trusted.jsonl` 28-set（注意 matcher 成本，需 timeout+控并发）；(b) 修 low-sev 空预测瑕疵；(c) ~~决定是否把 gold 回写纳入版本控制~~ **已解决**：数据集仍 gitignored，但 5 条 gold 增量导出为 tracked 幂等脚本 `process/gold_writeback_5apps.py`（按 content 判重、id 取 max+1、改前备份）。**fresh clone 跑一次即同步**：`python process/gold_writeback_5apps.py`。已验证：live 数据集上全 SKIP（幂等），pre-writeback 备份上全 APPLY 还原 id 18/16/17/19/27。

---

## 变异 catch-rate 尺：7-app × 2-mutant (CS+IX) 扩规模 (2026-06-10)

**动机**：方向 B 首个 mutation pilot 只跑 3-app × 1 CS mutant（catch_rate 0.333，n=3，pipeline 验证非测量）。本轮把硬化后的 harness（PR#8 / `c2ced91`）扩到 **7-app × 2-mutant**：`--mutants-per-app 2` 经 `QUOTA=[constraint,interaction,...]` 按 `k%len` 映射 → k=0→**CS**、k=1→**IX**（无需单独的 class flag）。命令为交接文档里的标准跑法（外层 `timeout 18000` 硬墙 + `--concurrency 2` + `--mutant-timeout 2400` + MiniMax-M3 独立 HTTP 判定）。3 个缓存的 CS mutant（0009/0037/0080）检测续跑、用 MiniMax 重判。

**结果（14 mutant 全 `valid`，0 invalid / 0 suspect / 0 超时 / 0 硬失败 / 0 僵尸服务器——硬化在 7-app 规模扛住了）**：

| 类别 | 抓到/总 | catch_rate |
|---|---|---|
| 总体 | 5/14 | **0.357** |
| CS | 2/7 | 0.286 |
| IX | 3/7 | **0.429** |

逐只（✅抓/❌漏）：CS 0009✅ 0089✅，0035❌ 0037❌ 0070❌ 0074❌ 0080❌；IX 0035✅ 0037✅ 0080✅，0009❌ 0070❌ 0074❌ 0089❌。归档于 `outputs/_mutation_probe/summary_7app_csix.json`（防下次 run 默认 `--out summary.json` 覆盖；旧 3-app 基线文件已被本轮覆盖，但那 3 条记录在新文件里完整复现）。

**观察**：
1. **复现性**：3-app CS 子集（0009/0037/0080）结果与首个 pilot 完全一致（0009 抓 3-0、0037/0080 漏 0-3）→ 指标稳定可重复。扩到 7-app 后 CS 0.333→0.286（新 4 app CS 仅 0089 抓到）。
2. **IX(0.429) > CS(0.286)**：约束/守卫逻辑（校验、guard、`<` vs `<=`）这类"沉默 bug"是 agent 的软肋，比交互可观察类更难抓。
3. **"漏"里多是近失**：好几个 ❌ 其实是检测抓到了**另一个真 bug** 而非注入那只——0035 CS 抓"空消息被接受"漏"空邮箱被接受"（同校验家族、相反字段）；0074 CS 报"拖拽手柄从不渲染"（比注入的反相逻辑更严重）。严格匹配**低估**真实查 bug 能力，这是变异尺"宁严勿松"的代价，与 [[gold-incompleteness-diagnostic]] 一致（指标只测"抓到特定注入"，不测"找到任何真 bug"）。
4. **印证开放问题**：0070 m1 有一票 `"unparseable verdict"`（`parse_catch` 裸大括号解析偏差），另两票一致 False、本例结论稳健，但解析 bug 确实存在 → 下一步 Phase 2 修。

**定论**：硬化后的变异尺在 7-app 规模上**干净跑通**（14/14 valid，零异常），给出方向 B 的第二根 gold-independent 刻度：总体 catch_rate 0.357、IX>CS。CS 偏低 + 大量"近失"共同指向检测的**约束类敏感度**与**匹配严格度**两个可改进面。

### 扩到全 4 类 FT+CT (2026-06-10，承上)

把同 7 app 跑到 `--mutants-per-app 4`（k=2→FT、k=3→CT），补齐 **全 4 类 = 28 mutant**，全 `valid`、零异常。CS/IX 续判（用 PR#9 修好的 `parse_catch`）**逐只复现** 2 类档的归档值、**零 unparseable**——live 验证了 Phase 2 的解析修复。（注：跑到一半曾手动暂停一次，按断点幂等续跑：只补 0070/0074/0080/0089 的 8 个 fresh FT/CT + 重判其 CS/IX，再本地聚合全 28 个 `result.json`，零额外 LLM。）

| class | 抓/总 | catch_rate |
|---|---|---|
| **FT** | 4/7 | **0.571**（最易） |
| IX | 3/7 | 0.429 |
| CT | 3/7 | 0.429 |
| CS | 2/7 | 0.286（最难） |
| **总体** | **12/28** | **0.429** |

逐 app×类格点（✓=抓）：0009 `✓·✓✓` / 0035 `·✓✓✓` / 0037 `·✓·✓` / 0080 `·✓✓·` / 0089 `✓·✓·` / **0070 `····` & 0074 `····`（4 类全漏，盲区）**。

**观察**：(1) 明确梯度 **FT(0.571) > IX=CT(0.429) > CS(0.286)**——功能性 bug 最易抓、约束类最难,印证 2 类档的 CS 软肋结论并扩展到全类。(2) **0070/0074 两个 app 4 类全漏**是最强的盲点信号(0070=工作流"禁用按钮仍可点"序列约束、0074=数据管理排序/拖拽)——这两个恰是验证-5 回写里**没碰过**的 app,值得后续重点白盒看。归档 `outputs/_mutation_probe/summary_7app_4class.json`。

---

## 方向 B 推广：28-set verify→回写 (Phase 4，2026-06-10)

把方向-B 的"发现→gold-blind 验证→回写"链从 7-app mini 集**推广到 28-record `_eval_trusted.jsonl`**(direction-2)。

**① 检测**：28 集里仅 7 个已有 P1-A 检测(p1exp);对**缺的 21 个**补跑 P1-A 检测(7 分片并行、`--hunt_rounds 0`、sonnet CLI、确定性端口),28/28 覆盖、0 失败分片。共收割 **34 个 `EX-NN` 离清单候选**,排除已回写的 5 个 → **验证队列 29**。

**② gold-blind 对抗验证(ultracode 工作流 `outputs/_p4_verify/verify_workflow.js`)**:每候选 pipeline **验证(3 视角:机制 file:line / 用户可观察 / instruction 是否承诺)→ 独立 skeptic 力图反驳**,合并裁决写 `outputs/_p4_verify/v_<sid>_<exid>.json`。58 agent / 2.37M tok / ~9min。**gold-blind 经审计零违规**(grep 全 agent transcript:0 次读 `WebTestBench.jsonl`/`_eval_trusted.jsonl`;只读 app `src/`)。持久化/刷新丢数据类按用户定的策略**逐个对各 app instruction 判**。

**③ 结果**:**21 确认(15 real + 6 likely),8 not_a_bug,0 refuted**。not_a_bug 多为"instruction 未承诺"的正确排除(持久化 0001-04/0003/0025/0100、0015 匿名重投、0047-02 重名、0072 订单、0076-01 大小写)——这正是 [[gold-incompleteness-diagnostic]]"对实判而非对 gold 判"的应用。

**④ 回写**:经用户检查点定 scope = **只回写 15 real**(6 likely intent-dependent,暂不写)。tracked 幂等脚本 `process/gold_writeback_p4_15.py`(分组支持一 app 多 bug、按 content 判重、id 取 max+1、改前备份 `.bak-gold-writeback-p4`),写入 13 个 app 的 gold。类分布 FT 10 · CS 3 · CT 2 · IX 0(注:写回的 15 real 里 IX 那条 0062 计为 interaction,CS 含 0001-03/0014-01/0076-02)。最严重:0056 `<SelectItem value="">` 整页崩溃(critical)。

**定论**:verify→回写链在 28-set 上跑通——从 21 个 app 的检测里,gold-blind 对抗验证净得 **15 条 gold 缺失的真 bug**(+5 先前 = 20 条跨 18 app),全部可由 `process/gold_writeback_*.py` 在 fresh clone 复现。**下一步候选**:(a) 6 likely 逐条定夺;(b) 0070/0074 变异盲区白盒深挖;(c) 在扩充后的 gold 上重测 matcher-voting recall。

## 开放问题 1+3：20-bug gold 复测 + 0070/0074 盲区白盒 (2026-06-10)

ultracode 双线工作流（87 agents；尾部 3 个收尾 agent 撞 session limit，从 transcript 恢复后主循环补完）。

**Q1 — P4 回写量化（13 records，K=3 双臂受控 A/B，旧 gold=`.bak-gold-writeback-p4`）：**
- Overall P/R/F1 **0.6095/0.3538/0.3979 → 0.6708/0.4108/0.4771**（+6.1/+5.7/+7.9pt，三项同涨：EX 预测由 FP 转 TP）。
- linkage 审计：**12/15 回写 item 计入 TP（10 条经 EX-NN）**；3 条失败全在 matcher 端（0020#17 错连到 PASS 项、0062#14/0088#11 有语义等价 EX-01 却未连）→ 「gold 正向预期 vs pred 故障现象」反向措辞在 K=3 下仍 ~20% 漏连，真实 recall 被低估 ~3 bug。
- K=3 跨运行漂移未归零：3/13 records 旧 item ±1 TP（0076 的 recall 降全是漂移）——看聚合，勿读单 record。
- 报告：`outputs/_q1_rescore/report.md`（audit_refined.json 为基线数据）；版本目录 `outputs/p1exp_p4{old,new}gold`。

**Q3 — 盲区白盒（8 诊断 + gold-blind 猎虫 + 39 张对抗验证票）：**
- 漏检诊断：0070 m0/m1/m2 = **mutant 无效**（CSS-only 被原生 disabled 属性中和 ×2、死代码 NavLink ×1，validity 判官误放行）；0074 m0 = **判官假阴性**（agent 已报 FAIL 引用变异谓词）。真 miss 仅 4 个：misjudged_pass ×3（文案/状态词未核对）+ not_exercised ×1（未对同字段二连点）。
- **catch_rate 修正：12/28=0.429 → 13/25=0.520**（FT .667 > CS=IX .500 > CT .429，CT 成最弱类）。`summary_7app_4class.json` 留档不改，引用以修正口径为准。
- 猎虫：**9 real + 2 likely 确认 / 2 反驳**。7 条 real & off-gold 回写候选（0070-C1/C5、0074-C1/C3/C4/C5/C7）+ 3 条 gold pass=True 翻转候选（0070#7/#8、0074#13）——**均待拍板，未回写**。0074 确认 bug 与其 4 个 missed mutant 落在同一批盲面（排序状态/搜索语义/编辑路径校验/空值文案），印证"检测盲区 = gold 缺失区"。
- 改进项：harness 加 manifestation/reachability 预检 + 判官部分匹配规则；检测提示词加「逐字读文本节点 / 状态词对照 / 同控件二次激活」三规则。
- 报告：`outputs/_q3_blindspot/report.md`（synthesis_data.json 含全部票据）。

## 开放项依次落地：Q3 回写 ×9 + matcher 极性规则 (2026-06-10)

**Step 1 — Q3 回写（7 real & off-gold）**：`process/gold_writeback_q3_7.py`（commit 2684440）。0070 +2（id17 一次性 save gate、id18 模拟终态矛盾）、0074 +5（id19 悬空 sortConfig、id20 搜索匹配存储值、id21 重命名绕校验、id22 import 流程缺失、id23 空日期 '' 非 null）。备份 `.bak-gold-writeback-q3`，幂等已验。

**Step 2 — 翻转候选裁决（追加不翻转）**：`process/gold_writeback_q3_edit2.py`（commit c1c0b1c）。0070 +2 条件项（id19→#8 的 filtered-list 绕过、id20→#7 的 rename 路径绕过）；理由：#7/#8 在 normal path 真实成立，翻转会把正确的 PASS 观察打成 FN，追加让两类 agent 都可被正确计分。0074#13/C6（likely 级）缓议不动。**gold 累计 = 29 verified bugs / 20 apps**（0070 现 20 items/9 bugs，0074 现 23 items/10 bugs）。

**Step 3 — matcher 极性盲匹配规则（branch `tune/matcher-inverted-phrasing`，merge c437bfa）**：`PROMPT_MATCH_ITEM` 新增规则 3——gold 正向预期 vs pred 故障描述的同面反极性对必须匹配。13-record 受控 A/B（同预测同 gold、K=3，对照 p1exp_p4newgold 基线）：
- 靶向 3 条全修复：0020#17 cov-pass→TP、0062#14 / 0088#11 unlinked→TP（全部经 EX-01）。
- 总体 P 0.6708→**0.8195**、R 0.4108→**0.4672**、F1 0.4771→**0.5571**。
- 唯一回归疑点 0076#17 经 6-run 对照（新旧 prompt 各 ×3）证伪：两臂均 0/3，EX-02 在姊妹 gold bug #9/#17 间摇摆，属 record 固有歧义 + matcher 跨时段漂移，与 prompt 无关。0021 的新 FP（CT-01→#16）是极性规则正确连接后暴露的 agent-vs-gold 真实分歧，非误连。
- 教训：MiniMax key 必须用完整值（`~/intership/minimax_api.md` 的 key 有 130 字符，截断版一度可用后被 401）；scoring 零分 fallback 也会写 score.json，分片成功检查必须验 `score_match_ids.json` 的 votes+matches。
- 工件：`outputs/p1exp_p4newgold_mfix/`（新 prompt 13-record 跑批）、`outputs/mfixstab_{new,old}{1,2,3}/`（0076 稳定性对照）。

## 隐患排查：衍生子集 jsonl 陈旧性 (2026-06-10)

回写只改主 `WebTestBench.jsonl`，所有已抽取的子集副本静默保留旧 checklist——`_eval_trusted.jsonl` 在 P4+Q3 后已 **20/28 条陈旧**（138 vs 167 bugs），若被打分引用会用旧 gold 低估 recall。处置：新增 tracked 守卫 `process/check_subset_staleness.py`（审计全部子集；冻结的 A/B 输入 `_gwb5_*`、`_p4q1/*` 拒绝刷新；`--refresh` 带备份重写）。已刷新 8 个非冻结子集（`_eval_trusted`、`_eval_mini` + 6 个历史消融输入），审计全绿。**纪律：每次 gold 回写后跑一遍该脚本。**

## 官方 7-app 基线（mini-7）落定 (2026-06-11)

用户决定：暂不立 28-set 基线，**以 mini-7（_eval_mini.jsonl，每类 1 app：0009/0035/0037/0070/0074/0080/0089）为当前工作基线**。预测复用 p1exp 检测产物（无需重测），测量系统 = 现行 gold（含 29 回写，7 app 计 58 bugs）+ MiniMax-M3 K=3 + 极性 prompt。版本目录 `outputs/p1exp_mini7/`。

| | P | R | F1 | coverage |
|---|---|---|---|---|
| **mini-7 基线 (当前)** | **0.7631** | **0.2909** | **0.3817** | 0.6794 |
| 旧 7-app 数字（votes=1/旧 gold/旧 prompt，作废） | 0.3214 | 0.1862 | 0.2289 | — |

by_class：FT 0.449 > CS 0.3429 > CT 0.1667 > **IX 0.0**（与突变尺梯度一致：交互/内容类是检测盲区）。per-record 两极：0035 F1 0.8235 最好；0070 R=1/9、0074 R=1/10 ——Q3 白盒回写的 9 条 bug 对 p1exp 多为 FN，**这是 gold 补全后的诚实盲区度量，不是退步**。注意新旧数字之差全部来自测量系统（同一批预测）；后续检测优化以 0.3817 为对照起点。配套 gold-independent 尺：7-app 突变 catch_rate 存档 0.429 / 修正口径 0.520（harness 预检落地后正式重基线）。

## mutation harness 预检 + 判官规则落地，基线重立 15/25 = 0.600 (2026-06-11)

branch `tune/mutation-precheck-judge`（merge `9aaedb6`），TDD 14 新测试，全套 131 绿。

**落地内容**：(1) `ml.precheck_mutant` 静态预检——reachability（从 src 入口 BFS import 图，拒死代码文件）+ manifestation（仅改字符串字面量且变更 token 全为 a11y 不可见 utility class 则拒）；接入 `run_one_mutant` 部署前，无效 mutant 不再烧 ~22min 检测。对 8 个已诊断 mutant 实战校验 8/8 与白盒诊断一致。(2) 判官 prompt 规则 2——被变异面的**部分/单向症状**计 caught（需是注入变更的直接后果）。(3) `scripts/mutation_revalidate.py`——从既有工件重derive基线（预检 + 3 票 MiniMax 重判），不重跑检测，指标可比。

**重立基线（`summary_7app_4class_v2.json`，旧 v1 存档不动）**：

| | v1 (0.429) | **v2 官方** |
|---|---|---|
| overall | 12/28 = 0.429 | **15/25 = 0.600** |
| FT | 0.571 | **0.833** (5/6) |
| CS | 0.286 | **0.667** (4/6) |
| IX | 0.429 | 0.500 (3/6) |
| CT | 0.429 | 0.429 (3/7) — 最弱类 |

validity 28→25（0070 m0/m1 css-only、m2 unreachable）；判官翻转 3 个且全部通过对抗审查：0074 m0（报告引用了被变异谓词，原判官假阴性）、0035 m0（`&&`↔`||` 单谓词翻转的另一方向，memory 在案的 near-miss）、0037 m2（EX-01 证据原文即注入症状 '36mo'，黑盒报症状不担根因归因义务，2-1 通过）。其余 22 个判决与 v1 完全复现，判官稳定。0.600 > 先前 0.520 预测：预测只含 validity+假阴性两项，partial-match 规则额外治好两个已记录 near-miss。CT 仍是真盲区，与 mini-7 gold 基线的 by_class（CT 0.167 / IX 0.0）互证——检测提示词三规则（逐字读文本节点/状态词对照/同控件二次激活）是下一杠杆。

## 检测提示词三规则 (dd3r)：变异尺 0.600→0.760，gold 尺被不完整性遮蔽 (2026-06-11)

branch `tune/detection-3rules`（commit `2eb4cab`+）。§2 Verification Logic 新增三规则（Q3 盲区诊断的对症药，靶向 0070 m3 / 0074 m1·m2·m3 四个真 miss）：**逐字读文本节点**（CT 项读区域内每个文本节点：次级标签/计数器/"X of Y"/单位/空态）、**状态词对照**（Yes/No、On/Off、asc/desc 必须与控件真实状态比对）、**同控件二次激活**（toggle/排序同控件连点两次验证 A→B→A）。

**变异尺（同 25 注入、鲜检测，sha256 防漂移）：19/25 = 0.760，+0.160**。逐类：**CT 0.429→0.714、IX 0.500→0.833**（双靶类大涨）、CS 0.667→0.833、FT 0.833→0.667（回吐）。翻盘 +7：0009 m1 IX、0037 m0 CS、0070 m3 CT、0074 m1 IX、0074 m2 FT、0080 m3 CT、0009 m0 CS(重跑)——**Q3 点名 4 真 miss 挽回 3**（0074 m3 状态词题没接住：注入在 Add/Edit 对话框标签，agent 只走了内联面）。回吐 −2 均非规则反作用：0037 m2 = 注意力置换（agent 看到 "36 months" 症状原文但归因给同场的真实年龄分类数据 bug）；0089 m2 = 探索路径方差（可选字段留空路径未行使）。

**mini-7 gold 尺：F1 0.3817→0.3390（P 0.763→0.563，R 0.291→0.255），靶类 CT/IX 持平 0.1667/0.0**。但 FP 审计揭示精度跌幅主体是测量假象：**新增 FP 仅 6 个，白盒核验 5 个 REAL**（0037 复数 `PetsPage.tsx:100`、0037 年龄组数据错分 ×3、0037 卡片缺 shelterName【挑战 gold#13 pass=True】、0009 EditListing 无角色守卫、0074 空态文案误导），第 6 个（0074 卡片标题无标签）事实准确但严重度存疑。0070 +0.30（Q3 回写过同类 bug 的 app，规则收益可见）；0080 −0.25 / 0009 −0.17 为单样本检测方差（0080 丢 gold#11 TP 与规则无关）。**双尺合读：规则真实提升了微文案/状态/交互敏感度，gold 尺低报是因为新发现多落在 gold 盲区**——与 [[gold-incompleteness-diagnostic]] 同构，且变异尺正是为这种场景而立。

**事故与硬化**：CLI 订阅额度 429 把首轮变异跑批烧穿（22/25 被静默标 invalid——deploy 成功但检测饿死）；0009 m0 在额度悬崖边降级运行（5 次 RateLimitEvent、84 vs 108 工具调用、CS-03 从未行使）误判 miss，干净重跑 caught。落地：`ml.is_rate_limited`（纯函数+测试）+ 探针 3-strikes 熔断（饿死 mutant 不写 result.json 保续跑性，commit `ab8af37`）；`mutation_probe --out-root`（同注入异 prompt 重测的标准姿势，commit `f491853`）。教训：**valid 数崩塌时先怀疑量测链路再怀疑被测物**；mini-7 跑批 + 变异跑批勿同窗叠放。0009 在新 prompt 下出合规产物费 3 次尝试（max-turns ×1、格式漂移 ×1）——格式方差仍是已知尾部风险（[[reverify-verdict-and-format-route]]）。

**待拍板**：(a) merge `tune/detection-3rules`？变异尺强烈支持（+0.160、双靶类 +0.29/+0.33），gold 尺的 −0.04 由已证实的 gold 不完整性 + 方差解释；(b) 5 REAL 回写候选 + gold#13(0037) pass=True 挑战（追加 or 翻转）；(c) 若 merge，变异尺官方基线更新为 `summary_dd3r_final.json` 0.760。工件：`outputs/dd3r/`（gold 尺）、`outputs/_mutation_probe_dd3r/`（变异尺，污染件归档 `_tainted_run1`）。

## dd3r 回写 ×4 + gold 尺重基线：双尺齐胜确认 (2026-06-11，承上)

**回写**（`process/gold_writeback_dd3r_4.py`，幂等、备份 `.bak-gold-writeback-dd3r`）：0009#19 traveler 直 URL 编辑旁路（gold#12 的旁路条件半面，0070 先例追加不翻转）、0037#18 复数文案、0037#19 卡片缺 shelter 名（gold#13 同款追加）、0074#24 搜索零结果空态误导。**撤回 2 项**：0037 年龄组错分（gold#17 宽措辞已覆盖，FT-02 的 FP 属 matcher 一预测一 gold 颗粒度问题）、0074 卡片标题标签（likely 级缓议）。**gold 现 33 bugs**。子集全刷新（_eval_mini/_eval_trusted/mini7 分片 ×3/_probe_0037），审计全绿。

**新 gold 受控双臂重打分**（同 gold 同 judge K=3，只差检测 prompt；版本 `m7ng_{old,new}`）：

| | oldP/newG | **newP/newG（新官方 gold 尺基线）** | Δprompt |
|---|---|---|---|
| P/R/F1 | 0.7810/0.2782/0.3717 | **0.6667/0.3096/0.4142** | F1 **+0.043** |
| CT f1 | 0.1667 | **0.4334** | **+0.267** |
| CS f1 | 0.3429 | 0.4048 | +0.062 |
| FT f1 | 0.4490 | 0.3544 | −0.095 |
| IX f1 | 0.0 | 0.0 | 0 |

**CT 类 gold 尺 +0.267 与变异尺 +0.286 互证**——三规则的微文案收益在两把独立尺上同向同量级；FT 回吐亦双尺同向（注意力置换）。EX-02→#19 验证旁路条件项无 #12 摆动。0009 平分 = 新增 TP 被 CS-03/04/05 三预测齐连 #9[ok] 的 FP 对冲——与 0037 FT-02 同属**「多预测争一 gold/颗粒度错配」matcher 问题**，已成为下一候选（⑤）最具体的两个靶样本。0035 −0.075 / 0080 −0.115 为既知单样本检测方差。**自此官方基线对 = gold 尺 0.4142（m7ng_new）+ 变异尺 0.760（summary_dd3r_final）**；旧 0.3817 与 0.600 作废。

## matcher 缺陷锚定规则：FAIL 预测按缺陷匹配，gold 尺基线 0.4142→0.4480 (2026-06-11，承上)

branch `tune/matcher-defect-anchor`（merge `90e5251`）。**问题**：`_parse_pred_items` 只把 checkbox 单行描述喂给 matcher，Bug Report 完全不可见——FAIL 预测按表面措辞落到 ok 项，其实际缺陷属于姊妹 bug 项（dd3r 双靶样本：0037 FT-02 年龄错分被连 #1[ok] 而非 #17[BUG]；0009 CS-05 双订缺陷被连 #9[ok] 而非 #10[BUG]）。**修法两件套**（TDD ×5，138 绿）：解析器对 FAIL 项折叠首个 Issue/Actual 为 `| defect …`（200 字截断、单行、PASS 项不动）；`PROMPT_MATCH_ITEM` 规则 4——FAIL 预测按其观察到的缺陷锚定 gold，胜过表面相似（原 4/5 顺延 5/6）。

**受控 A/B**（同预测同 gold K=3，`m7dm_{old,new}` vs `m7ng_{old,new}`）：官方臂（新 prompt 预测）**F1 0.4142→0.4480、P 0.6667→0.7381、R +0.021**，全部来自 0037 FT-02→#17 翻转（FP 消除 + EX-01 同项共存），**其余 6 记录字节级零扰动**。旧臂 −0.007 由 0035 三处洗牌引起 → **6-run 对照**（新旧 matcher 各 ×3）：IX-04 六跑全稳 #5、F1 全 0.8750——洗牌系一次性投票漂移，非规则效应。样本 2（0009 CS-05→#9 不动）判定为 gold 内部 #9/#10 需求重叠，matcher 选择可辩护，不再追打（matcher 看不到 gold 的 pass 态，无法也不应偏置向 bug 项）。

**度量语义备忘**：混淆矩阵只遍历 gold 项——**unmatched 的 FAIL 预测对 P/R 完全不可见**（只影响 coverage）；精度损失只来自「FAIL 预测连到 gold-ok 项」。此前 FP 普查把 unmatched 也计为 FP 系口径偏差，结论不受影响（那些是该回写的真 bug）。

**官方基线对更新：gold 尺 = 0.4480（`outputs/m7dm_new`，defect-anchor matcher + 33-bug gold + K=3）+ 变异尺 = 0.760**。0.4142/0.3817 作废。

## 时间漂移 gold 修复：0002/0006 追加 3 项（不翻转） (2026-06-11，承上)

[[gold-time-drift-invalidity]]（2026-06-03 在案）的处置落地，机制全部于今日白盒复核。**修复策略 = 0070 追加不翻转**：agent 可自建 2026 日期的事件/记录而正常通过 browse/dashboard——翻转 #6/#7/#8(0002) 或 #21(0006) 会惩罚正确的新数据观察；在任何未来时钟下都稳定成立的缺陷是「**预置种子数据永不可达/不可见**」，以条件 bug 项入账（`process/gold_writeback_timedrift_3.py`，备份 `.bak-gold-writeback-timedrift`）：

- **0002#20 (FT)** 种子事件访客侧全线不可见：所有访客列表路径（浏览/搜索/类别/日期筛选）终于 `>= new Date()`（Index.tsx L43）而种子全 2025 日期 → 开箱即 "No events found"、搜索既有事件 0 结果。
- **0002#21 (CS)** Featured 区无日期窗（Index.tsx L15-16）→ 已过期活动在访客首页可见——gold#13 的旁路违规半面（#13 保持 pass=True，主网格的过滤真实成立）。
- **0006#22 (FT)** Reports 月份下拉只给最近 12 个相对月（Reports.tsx L13-21）→ 含全部种子交易的 2024-10..12 永不可选、Reports 永不能汇总既有数据。**修正旧记忆的过宽论断**：TaxSummary 年份下拉跨最近 5 年、2024 可达（TaxSummary.tsx L15-18），不在缺陷内。

**gold 现 36 bugs**。7 个含 0002/0006 的存活子集全刷新、审计全绿。0002/0006 仍排除在 `_eval_trusted`/mini-7 之外（冻结基线集不动）；此修复使其 gold 对未来全集跑批有效。队列 ①-⑥ 全部完成。
