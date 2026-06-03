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
