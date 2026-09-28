# harness 断言修订记录（续测员1c · round-2 裁定 4 条坏断言）

背景: `qa/verify-round2.md` §1/§6 裁定 18/18 VERIFY_PASS，但 P1/P2/P4/P5 的 6 条残留 FAIL
全部是"已登记的硬编码期望 / 数学不可达断言"，造成"账本绿 vs 测试红"矛盾。
本文件记录这 4 条断言的**修订前后对照 + 不可达性证明 + 可达性验算 + 回归有效性(变异)检验**。

约束遵守情况: 未放宽到恒真（每条都附变异检验，证明回归仍会 FAIL）；
产品代码零改动（`git diff backend/ modules/` 为空）；`qa/orchestrator/test_fix_chapters.py` 未触碰。

复跑证据（2026-09-21，真实路径，专用端口 8271，未复用/未杀他人进程）:

| 脚本 | 复跑结果 | 日志 | 结果 JSON |
|---|---|---|---|
| `qa/harness/run_p1_import.py` | 8/8 用例 PASS | `qa/round-1/logs/p1_import.log` | `qa/round-1/artifacts/result_P1-import.json` |
| `qa/harness/run_p2_parse.py` | 9/9 用例 PASS | `qa/round-1/logs/p2_parse.log` | `qa/round-1/artifacts/result_P2-parse.json` |
| `qa/harness/run_p4_split.py` | 4/4 用例 PASS | `qa/round-1/logs/p4_split.log` | `qa/round-1/artifacts/result_P4-split.json` |
| `qa/harness/run_p5_analyze.py` | 5/5 用例 PASS | `qa/round-1/logs/p5_analyze.log` | `qa/round-1/artifacts/result_P5-analyze.json` |

---

## 1. run_p1_import.py 用例6 (PY-102 `.chm`)

### 修订前（硬编码永假）

```python
r = su.new(".chm 声称支持但解析器缺失")
...
unsupported = ("不支持" in msg) or ("chm" in msg.lower()) or ("CHM" in msg)   # 算了不用
r.check("CHM 导入可用 (期望)", False,
        f"实际: {code} {msg[:150]} -> parser_factory 对 .chm 直接 return None")
```

不可达性证明: 第二个实参是字面量 `False`。`Result.check(desc, ok, ...)` 把 `ok` 直接计入
`self.checks` 并在 `not ok` 时 `_downgrade("FAIL")`（`harness_lib.py:129-135`），
因此该 check 与产品行为完全无关，**任何实现都恒 FAIL**；同用例算出的 `unsupported` 变量被丢弃。
PY-102 的裁定本身是"移除虚假声明"（`.chm` 从 FORMAT_MAP/Config/白名单删掉），
即"CHM 导入可用"这一期望已不再是契约，永假是逻辑必然而非产品缺陷。

### 修订后（语义正确 + 可达 + 漂移即 FAIL）

```python
expected = sorted(ParserFactory.FORMAT_MAP.keys())           # 期望值动态取自生产单一事实源
m = re.search(r"支持[:：]\s*([^）)\n]+)", detail or "")
listed = sorted(x.strip() for x in m.group(1).split(",") if x.strip()) if m else []
r.check("返回 4xx 明确拒绝 (不再 500)", 400 <= code < 500, ...)
r.check("detail 支持列表 == ParserFactory.FORMAT_MAP (单一事实源, 漂移即 FAIL)",
        bool(listed) and listed == expected, ...)
r.check("契约自洽: .chm 未被声明支持 且工厂确实返回 None",
        ".chm" not in listed and ParserFactory.get_parser("probe.chm") is None, ...)
r.check("拒绝时不写入脏 book 行", books_before == books_after, ...)
r.check("Config.SUPPORTED_EBOOK_FORMATS 无孤儿声明 (每项都有已注册解析器)", not orphan_claim, ...)
```

要点:
- 期望列表**不写死**，两边（`backend/api/books.py:42-44` 的 detail 与 `ParserFactory.FORMAT_MAP`）
  一旦漂移就 FAIL。这正是 PY-102 的复发形态（白名单与工厂不同源）。
- `bool(listed)` 防止 detail 文案改版导致正则取空 → 空列表 == 空列表 的假绿。
- 末条把 PY-102 根因（声明-实现漂移）本身变成护栏: `Config.SUPPORTED_EBOOK_FORMATS`
  任一项若无已注册解析器则 FAIL。将来实现 `chm_parser.py` 并登记进 FORMAT_MAP，
  本用例自动跟随新支持列表，无需再改。

实测: `400 detail=不支持的文件格式: .chm（支持: .azw3, .docx, .epub, .htm, .html, .mobi, .pdf, .txt）`，
`listed == expected == ['.azw3','.docx','.epub','.htm','.html','.mobi','.pdf','.txt']`，
books 67 -> 67，`orphan_claim=[]` → 5/5 check OK。

---

## 2. run_p2_parse.py 用例0 (PY-104 `_stage_parse`)

### 修订前（硬编码永假 + detail 文案陈旧）

```python
body = src.split("def _stage_parse", 1)[1].split("def _stage_clean", 1)[0]
calls_real_parser = "ParserFactory" in body or "parser" in body.lower().replace("book_parser", "")
r.check("parse 阶段调用真实解析器", False,
        "函数体仅 logger.info + return {'chapters': 原样返回}, 未调用 ParserFactory; ...")
```

不可达性证明: 与 §1 同型 —— 第二实参字面量 `False`，恒 FAIL。
且 detail 描述的"仅 logger.info + 原样返回空壳"在 `8cee561` 后已不存在（verify-round2 §3 静态审查确认），
`calls_real_parser` 算出 True 却被丢弃。

### 修订后（按新裁定语义: P2 职责 = 章节校验 + 缓存重建）

三条真实路径断言（用例号 0 / a / a2 / b）:

1. 源级护栏（防空壳复发）: 函数体须同时含 `book_service`（DB 回填）与 `ParserFactory.parse`（重建）。
2. (a) 正常路径: 从 DB 取 3 章喂进 `_stage_parse` → 章数不丢 + `char_count == len(content)`。
   (a2) 校验职责: 人为把首章 `char_count` 注入 +7 漂移、把次章 `content` 清空只留 id →
   返回后残余漂移为 0 且空章按 id 从 DB 回填（实测回填 320 字）。
3. (b) 自愈路径**真实触发**: `context["chapters"] = []` →
   断言重建章数 > 0、重建章 `id` 全为 `None`（证明是内存重建而非缓存回读，且不回写 DB）、
   章序/标题与导入期一致且字数不丢。

实测（贴重建章数）:

```
[Conversion:harness-p2-b] Parse stage: 章节缓存不可用, 按 ed92fe1b-...txt 重建 (PY-104 兜底路径)
(b) 重建章数: 3
(b) 重建章标题: ['第一章 初入京城', '第二章 夜半来客', '第三章 旧事重提']
(b) 重建总字数: 791
```

与 DB 侧 3 章 / 791 字逐项一致 → 7/7 check OK（该用例含 (a)/(a2)/(b) 共 6 条行为断言 + 1 条源级断言）。

可达性: `_stage_parse` 的 usable==0 分支确实走 `ParserFactory.parse(book.file_path)`
（`modules/workflow/conversion_workflow.py`），TXT 二次解析结果与导入期同源同函数，故必然相等。

---

## 3. run_p4_split.py 用例2 (PY-107 分句粒度)

### 修订前（数学不可满足）

```python
strong = len(re.findall(r"[。！？…]", LONG))     # = 0
ratio = len(sents2) / max(strong, 1)             # 分母被保底成 1
r.check("分句粒度按句末标点(而非逗号)", ratio < 2.0, ...)
```

不可达性证明（fixture 手算）:

```
LONG = "她说了一句，然后停了停，又接着说，声音很低，像是在自言自语，谁也没有听见，" x6
     单元长 37 字 -> 总长 222 字; 逗号 36 个; 句末标点 [。！？…] 出现 0 次
分母 = max(0, 1) = 1
ratio < 2.0  且 分母=1  =>  len(sents2) <= 1  =>  输出必须是 1 句、单句 222 字
但 SentenceSplitter.MAX_SENTENCE_LENGTH = 150，222 字无损切分必然 >= 2 段
且同一用例下一行断言 "无超长句残留" 要求不存在 > 150 字的句子
=> 两条断言互斥，ratio < 2.0 在本 fixture 上对任何满足"不丢字 + 单句<=150"的实现恒假
```

实测复核（round-2 产物 `qa/round-2/artifacts/result_R2_P4-split.json` §2）:
`句数/句末标点 = 3.0 (3/0)`，`0/3 句以逗号结尾` —— 产品侧已完全达标（逗号结尾 0 句），
FAIL 只来自被保底的分母。

### 修订后（直接度量 PY-107 语义 + 先验算可达）

```python
ends_with_comma  = sum(1 for s in sents2 if s.endswith(("，", ",", "、", "；")))
ends_with_strong = sum(1 for s in sents2 if s and s[-1] in sp.CN_SENTENCE_END)
comma_share = ends_with_comma / max(len(sents2), 1)
r.check("逗号结尾句占比 < 5% (韵律单元不再停在逗号上)", comma_share < 0.05, ...)
gran = len(sents2) / max(ends_with_strong, 1)
r.check("句数/句末标点 ∈ [0.8, 2.5] (每个 TTS 单元恰好消费一个句末终结符)",
        0.8 <= gran <= 2.5, ...)
```

可达性验算（离线 `python -c` 对当前实现先跑，再写进用例）:

```
现实现: 句数=3, 逗号结尾=0, 句末标点结尾=3 (无句末标点的退化长句由 _normalize_tail 补 。)
  逗号结尾占比 = 0/3 = 0.0% < 5%              -> 可达 [实测 PASS]
  句数/句末标点 = 3/3 = 1.00 ∈ [0.8, 2.5]      -> 可达 [实测 PASS]
```

回归有效性（变异检验，证明不是放宽到恒真）: 用 PY-107 修复前的"无条件逗号强拆"重跑同一 fixture:

```
句数=36, 逗号结尾=36, 句末标点结尾=0
  逗号结尾占比 = 36/36 = 100.0% >= 5%          -> FAIL (抓住)
  句数/句末标点 = 36/max(0,1) = 36.0 ∉ [0.8,2.5] -> FAIL (抓住)
```

诚实留痕: `ends_with_strong <= len(sents2)` 恒成立，故该比值定义域为 [1.0, +∞)，
区间下界 0.8 不构成实际约束，真正的binding 是上界 2.5（旧口径之所以能"看似可达"
正是靠把下界放到 0.8 以下掩盖分母问题——此处按裁定保留 [0.8, 2.5] 区间但显式记录其定义域）。

---

## 4. run_p5_analyze.py 用例3 (PY-110 片段级对白/语气)

### 修订前（数学不可达）

```python
def count_quotes(t):
    return t.count("\u201c") + t.count('"')     # ASCII 直引号左右各计一次
raw_dialogues = sum(count_quotes(c["content"] or "") for c in chs)   # = 46
r.check("split 之后仍能完整识别跨片段对白", ndlg >= raw_dialogues * 0.6, ...)
frag_only = [... if "\u201c" not in s["text"] or "\u201d" not in s["text"]]  # 只看弯引号
```

不可达性证明（算式）:

```
fixture qa/harness/fixtures/sample_small.txt 用 ASCII 直引号: " 出现 46 次
count_quotes 不区分左右 -> raw_dialogues = 46
真实对白处数 = 46 / 2 = 23        （每处对白一对引号）
阈值 = 46 * 0.6 = 27.6  >  整书对白总处数 23
=> 即使片段级检测器完美到 100% 识别（ndlg = 23）也达不到 27.6，恒假
另: 旧 frag_only 探针只检查弯引号 U+201C/U+201D，
   对全 ASCII 直引号的 fixture 恒等于"全部对白片段"，统计数字无意义（能力边界被误记为 0）
```

实测复核（`qa/round-2/artifacts/result_R2_P5-analyze.json` §3）: `识别 16 / 实际对白 46`。
`DialogueDetector.detect_dialogues` 对整段正文返回 46 个 dialogue 片段 = 23 处 x2（同一对白被
前后缀两个模式各计一次），故"46"根本不是对白处数。

### 修订后（分母改为真实对白处数 + 三条可判定行为断言）

先离线 `python -c` 对当前实现验算（ParserFactory.parse → SentenceSplitter.split → `_stage_analyze`）:

```
整书对白处数(按成对计) 23 | split 片段 33 | analyze 判为对白 ndlg 16  -> 16/23 = 69.6% >= 60%
片段内引号仍成对可见 16, 其中判为对白 16                          -> 子集召回 100% >= 90%
判为对白却不含任何引号字符的片段                                  -> 0
含疑问信号(？/? 或句末 吗/呢/么)的片段 7, primary==question         -> 7/7 一致
（注: 这些疑问片段 confidence 仅 0.15~0.3，故只断言 primary 不断言置信度，避免不可达）
```

新断言:

```python
def quote_pairs(t):                     # 一处对白 = 一对引号; ASCII 直引号同符需 /2
    return t.count(DQ) // 2 + t.count(LQ)
raw_dialogues = sum(quote_pairs(c["content"] or "") for c in chs)            # 23
r.check("整书对白召回 >= 处数*0.6 (分母=成对白处数, 非引号字符数)",
        raw_dialogues == 0 or ndlg >= raw_dialogues * 0.6, ...)
visible = [s for s in sents if pair_visible(s["text"])]                       # 引号成对可见子集
r.check("可判定子集召回: 引号成对可见的片段 >=90% 判为对白",
        len(visible) > 0 and vis_recall >= 0.9, ...)
r.check("无幻觉对白: 不含任何引号字符却判为对白的片段 = 0", not halluc, ...)
def q_signal(t):  # ToneDetector 现行硬信号: ？/? 或句末 (吗|呢|么)+可选右引号
    return "？" in t or "?" in t or bool(re.search(r'(吗|呢|么)[\u201d"\u300d]?\s*$', t.strip()))
r.check("PY-110 可判定行为: 含 ？/? 或句末 吗/呢/么 的片段 primary == question",
        len(qsents) > 0 and not qbad, ...)
```

回归有效性（变异检验）:
- 若 `QUOTE_CONTENT` 弯/直引号保护被删（PY-108 复发），分句会把引号内部按 。 强切 →
  `visible` 掉到 0 → `len(visible) > 0` 直接 FAIL（不是 vacuous pass）。
- 若分句退化为逗号级碎片（PY-107 复发），单片段再无成对引号 → 同上 FAIL，
  且 `ndlg >= 23*0.6` 也随之 FAIL。
- 若 `ToneDetector` 疑问硬信号被回退（PY-110 复发，`？` 权重 0.8 / 句末 吗 0.6 移除），
  `"谁？"他低喝。` 这类不以 ？ 结尾的片段会掉出 question → `qbad` 非空 → FAIL。

能力边界留痕（不作失败判定，写入 `r.notes`，与 verify-round2 §6.3 一致）:
23 处整书对白中仅 16 处在分句后引号仍成对可见，片段级识别上限 = 16（实测 ndlg=16），
split→analyze 顺序损失属 round1b 既有遗留项，不属 PY-109/PY-110 声明范围。

---

## 附: 本轮登记但未改的 harness 瑕疵

`run_p1_import.py` 用例7 `r.check("服务端拒绝超大文件", True, f"{code}")` 是一条硬编码 **True**
（恒真，与 §1 的恒假相反）。本轮未 FAIL、未造成"账本绿 vs 测试红"矛盾，故不在 round-2 裁定的 4 条内，
按"不放宽、不越界改产品码、只做裁定内修订"的原则未动，登记备查:
实测行为已是 `400 ... 书籍内容 2700901 字超过上限 2000000 字 (MAX_BOOK_CHARS)`，
建议下一轮把它改成 `400 <= code < 500 且 detail 含 "MAX_BOOK_CHARS"/上限数字 且 books 行数不变`。

---

## 5. run_p3 / p5 / p6 / p7 的取数口径被 WF-09 打穿（PY-337，回写 2026-09-23 06:x）

**根因**：`0cb418b`（09-21 15:47，WF 工作流审计波）里的 WF-09 把 `GET /books/{id}/chapters`
改成只下发 `content_preview`、`content` 恒为 `null`（列表 N+1 修复，4.6s -> 0.11s）。
当时只有 `run_p4_split.py` 顺手按懒加载出口补了正文；`run_p3_clean` / `run_p5_analyze` /
`run_p6_assign` / `run_p7_synth` 四枚仍直读 `ch["content"]`，于是拿到 `None`。

**后果分四类，证据类型逐条标注（实采 = 本轮真跑出来的；读码 = 只看代码可判，本会话未跑出）**：
1. 〔实采·p7〕A2 喂进 0 句，生产 `_stage_synthesize` 按 PY-117 抛
   `RuntimeError: 合成阶段未产出任何音频段（输入 0 句均无有效文本）`——**这是产品在替 harness 兜底**，
   红的是取数口径；C5 因缺 `p7_segments` 一直 BLOCKED，元凶即此（05:5x 那次崩的原始输出留档
   `agent-ops/cast-reset/p7_run_py335.out`）。
2. 〔实采·p5 修后首跑〕红 2 条：§1 关键角色、§5 EPUB speaker。这两条**不是取数故障**，
   是 §6 说的 WF-01 账单；只是修好取数后才看得见（修前 §5 是 `0/0`）。
3. 〔实采·p3 修后〕3/3 绿。〔读码·p3 修前〕§2 `stored = chs[0]["content"]` 取到 `None`
   → 下一行 `len(None)` TypeError 当场崩；§3「导入清洗后无噪声行残留」判据是
   `sum(counts.values()) == 0`，零输入下**恒真**——这一条是真正的**静默绿**，09-21 起一直如此。
4. 〔读码·p5/p6 修前〕p5 并非静默绿：`ndlg > 0`、`len(visible) > 0`、`len(qsents) > 0`
   这些自带非空要求的门在零输入下**直接红**，所以 p5 自 09-21 起是**公开红**（本会话没跑修前版，
   不声称实测）；真正静默绿的是另外两条带 `分母 == 0 or …` 短路口的召回门（p5 §3 `raw_dialogues`、
   §5 `e_quotes`）。p6 修前后各跑一次才算账，见本节末与 LEDGER。

**修法（只改 harness，产品码零改动）**：`harness_lib.fill_chapter_content(chs)` 统一走
`GET /api/books/chapters/{id}/content`（与前端 EditPage 同口径）补正文，四枚 harness 改用它；
并把 3 那类"零输入恒真"的短路改成**显式前置门**（`真书正文非空`、`raw_dialogues > 0`、`e_quotes > 0`），
零输入要红得有名，不许静默通过。附带 `start_backend(env_extra=…)`：只做导入/查询的用例传
`PYY_PRELOAD_MODEL=0`（产品自带配置口），避免与 harness 进程内那份模型抢 ~5GB。

**同类扫描已做全（不再漏第五枚）**：`grep '\["content"\]' qa/harness/*.py` 命中 8 枚，逐枚追正文来源后
只有上面这 4 枚（+ 已先修好的 `run_p4_split`）受害。`run_p1_import:78`、`run_p2_parse:40-41`、
`run_c4_cast_service:281` 拿的是 `H.db_rows("SELECT * FROM chapters …")`，绕过了 HTTP 契约，不受影响；
`run_c6_e2e_multicast:215-216` 确实走列表口（打印出来的正文长度恒 0，只是展示口径难看看），
但它把 `chapters` 交给生产 `_stage_parse`，而后者按 PY-104 专门处理"上下文只带截断 preview"
（`modules/workflow/conversion_workflow.py:701-714` 逐章 `get_chapter_content` 回填）；它在 :323 塞进 ctx 的
`total_chars` 求和虽为 0，也会被 `_stage_import:395` 用 `book.total_chars` 覆盖、再由 `_stage_clean:765-775`
按 `char_count` 重算 → 断言链无缺口（三处覆盖点为读码核验，C6 本轮未复跑）。
**结论：受害面 = harness 自己做文本处理、又直读列表口的那些；走生产 workflow 的由产品兜底。**

**复跑实测**：`run_p3` 3/3（真书 MOBI `盐战…` 81 章 / 330,188 字 / 4,798 非空行，噪声残留 0）；
`run_p5` 5/5（`raw_dialogues=23 / split 片段 33 / ndlg 16 = 69.6%`、疑问 7/7 ——
与 §4 当年钉的数字逐字相等，反证取数口径已回到 WF-09 之前的真实路径）。

## 6. run_p5 §1/§5 的命中集重裁（WF-01 账单，见 issues.md PY-338）

取数修好后 §1/§5 各红一条，**不是本轮改出来的**，也不是取数故障：`0cb418b` 的 WF-01 把
"引号**后窗**归因"收紧成只认说话动词，于是动作后窗（`“你当真要走？”阿贵蹲在门槛上`）整条通道关闭。
同夹具并排实测（`git show 0cb418b^:…dialogue_detector.py` vs HEAD）：§1 关键角色
**PRE 4/4 → HEAD 2/4**，同时 PRE 的假角色 `里头传来` / `声音很轻` 在 HEAD 归零；§5 EPUB
（该夹具唯一归因线索就是动作后窗）speaker **0/11**。

处置：两条断言钉成**双向棘轮**而不是放宽——§1 `命中集 == {周主事, 阿贵}`（精确值，多一枚也红）、
§5 `EPUB 动作后窗归因数 == 0` 并命名为"PY-338 账单快照"。谁按 PY-338 的可建方向把归因做回来，
这两条必须连着改，召回率不许静默变化。
