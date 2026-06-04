# 可信评测集（阶段 0 产出）

> **日期**：2026-06-04
> **用途**：在改 detection/checklist prompt 前，先有一个 gold 未漂移、类目均衡、Constraint/Interaction 覆盖足的固定评测集，否则在 stale gold 上看 F1 = 自欺（见 `[[gold-time-drift-invalidity]]` / `gold-bug-recall-research.md`）。
> **文件**：`data/WebTestBench/_eval_trusted.jsonl`（28 条，gitignored，可由下方脚本重建）

---

## 选集方法

1. **漂移过滤（保守、宁可错杀）**：宽关键词正则扫描 gold-bug（`pass:False`）内容，命中时间敏感词（today/current/upcoming/deadline/expir/countdown/remaining/this week·month·year/real-time/live/now/schedule/calendar/future/elapsed…）即标为漂移嫌疑，排除。
2. **硬排除已确认漂移**：`0002`、`0006`（来自记忆，已人工确认 gold 失效）。
3. **干净池**：100 → **67 条**。
4. **均衡抽样**：7 个 category 各取 4 条，类内按"Constraint+Interaction gold-bug 数"降序优先（即优先选最能测出你最差两类的记录）。→ **28 条**。

## 评测集构成

| Category | 记录 |
|---|---|
| Commerce | 0009, 0020, 0062, 0072 |
| Data Management | 0021, 0056, 0066, 0074 |
| Presentation | 0024, 0035, 0046, 0060 |
| Search | 0001, 0003, 0037, 0094 |
| Tool | 0025, 0047, 0076, 0080 |
| User-Generated Content | 0015, 0064, 0089, 0100 |
| Workflow | 0014, 0052, 0070, 0088 |

**gold-bug 类分布**：functionality 56 / **constraint 48** / **interaction 28** / content 6
（CS+IX 共 76 个 gold bug——足够测出对最差两类的改进。）

## ⚠️ Caveat（必读）

- **关键词判漂移不可靠**：同一批记录用宽/紧正则结果不一致（0002/0006 在紧正则下漏标）。本集用宽正则 + 硬排除，是"宁可错杀"的近似，**不是真值**。
- **真正严谨的漂移验证**需重新部署 app、逐条复现 gold-bug——成本高，未做。若某条改进结果异常，先怀疑该记录 gold 本身。
- **28 条仍是子集**：迭代用。重大结论建议再在 67 条干净池上确认一次（检测阶段是 150 轮浏览器会话，很贵，故不每次跑全量）。
- 本集**未读 gold 答案做任何选择**（只用了 `pass` 布尔位和 class 标签做漂移过滤与均衡），符合 `[[no-cheating-except-ground-truth]]`。

## 重建脚本

```python
import json,re,collections
recs=[json.loads(l) for l in open('data/WebTestBench/WebTestBench.jsonl')]
byidx={r['index']:r for r in recs}
TIME=re.compile(r'\b(today|tomorrow|yesterday|current(ly)?|upcoming|deadline|expir|expire|expiry|expired|countdown|due[- ]?date|remaining|days?\s+(left|until|ago)|this\s+(week|month|year)|real[- ]?time|live|now|schedule|calendar|date\s?picker|future|past|elapsed|age\b)',re.I)
KNOWN_DRIFT={'WebTestBench_0002','WebTestBench_0006'}
def drift(r):
    if r['index'] in KNOWN_DRIFT: return True
    return any(c.get('pass') is False and TIME.search(c.get('content','')) for c in r['checklist'])
clean=[r for r in recs if not drift(r)]
def csb(r): return collections.Counter(c['class'] for c in r['checklist'] if c.get('pass') is False)
bycat=collections.defaultdict(list)
for r in clean:
    cb=csb(r); bycat[r['category']].append((cb.get('constraint',0)+cb.get('interaction',0), r['index']))
ids=[]
for cat in sorted(bycat):
    ids+=[x[1] for x in sorted(bycat[cat], key=lambda x:(-x[0],x[1]))[:4]]
ids=sorted(set(ids))
open('data/WebTestBench/_eval_trusted.jsonl','w').write('\n'.join(json.dumps(byidx[i],ensure_ascii=False) for i in ids)+'\n')
```

## 迷你集（个位数，迭代首选）

> **文件**：`data/WebTestBench/_eval_mini.jsonl`（**7 条，全 7 类各 1 条**）
> 用于快速、低成本的 baseline-vs-treatment 对比；28 条集留作阶段性确认。

选法：在 67 干净池里，每个 category 取 **Constraint+Interaction gold-bug 最密集**的 1 条（让最差两类的 recall 有足够分母，避免单条 0/1 抖动）。

| 记录 | Category | CS | IX | FT | CT | 总bug |
|---|---|---|---|---|---|---|
| 0037 | Search | 2 | 1 | 0 | 1 | 4 |
| 0089 | User-Generated Content | 4 | 1 | 1 | 1 | 7 |
| 0009 | Commerce | 1 | 3 | 4 | 0 | 8 |
| 0080 | Tool | 2 | 2 | 3 | 0 | 7 |
| 0074 | Data Management | 2 | 2 | 1 | 0 | 5 |
| 0035 | Presentation | 2 | 1 | 5 | 0 | 8 |
| 0070 | Workflow | 2 | 1 | 1 | 1 | 5 |
| **合计** | 7类 | **15** | **11** | 15 | 3 | **44** |

⚠️ 7 条的 F1 抖动比 28 条大——**只用来快速看方向（CS/IX recall 是否动、FP 是否暴涨）**，确认有效后必须到 28 条（或 67 池）复测再下结论。

## 怎么用

```bash
# baseline（现状）
python eval/run_agent.py --agent claude_code --data_jsonl_path data/WebTestBench/_eval_trusted.jsonl \
  --version baseline_trusted ... && \
python eval/scoring.py --dataset_path data/WebTestBench/_eval_trusted.jsonl --version baseline_trusted ...

# 改 prompt 后用新 version 跑同一集，对比 F1 / by_class（重点看 constraint、interaction）
```
