# UNIMPLEMENTED — 语音助手交互功能待办

**本文件只描述「现在还没做的」。** 设计文档是 `spec.md`，使用说明是 `README.md`，
部署与排障是 `deployment.md`。这里不写已完成的东西。

---

## 给 agent 的强制指引

1. **动手前必读。** 在实现任何用户可感知的功能之前，先读第 0 节（既有约束）和第 1 节
   （前置改造）。第 1 节的几项是横切的——不先做，第 2 节的功能只能做一半，做完还会被推翻重做。
2. **做完就删。** 功能通过验收后，**回到本文件删除该条目**。部分完成就改写成剩余部分，
   不要把已完成的条目留在文件里当纪念。
3. **同一次提交里同步文档。** 删条目的那次提交，要同时：把新能力写进 `README.md`（用户视角：
   能说什么、有什么限制）、`deployment.md`（配置项、排障）、以及 `spec.md` 的对应小节与状态标记。
4. **发现新缺口就补进来。** 调试或实现过程中发现的新缺口，写进对应小节，注明出处。
5. **自证。** 每条都给了「验收标准」。实现后用它自证，并在 `tests/unit` 或 `tests/integration`
   补一个回归测试。测试模式见第 0 节最后一行。

---

## 0. 动手前必读的既有约束（都是踩过的坑）

| 约束 | 出处 | 违反的后果 |
|---|---|---|
| **TTS 只能念中文**。`vits-icefall-zh-aishell3` 的词表 66 377 条里**拉丁词条为 0** | `winvoice/audio/tts.py`、`models/tts/*/lexicon.txt` | sherpa 逐词丢 `OOV ... Ignore it!`，用户听到一句话中间空掉 |
| **用户可见文本走 `ToolResult.message`，机器文本走 `error`** | `winvoice/contracts/messages.py:183`、`pipeline.py:350` | 英文错误被念出来 = 上面的 OOV 刷屏 |
| **数字必须靠 `rule_fsts`**（`number.fst`/`date.fst`/`phone.fst`）。引擎已接，模型目录缺这几个文件就会失声 | `winvoice/audio/tts.py` | 「调到90」被念成「调到」 |
| **LLM 永不选工具**，只输出 intent + args | `winvoice/llm/grammar.py`、`pipeline.py:319` | 破坏整个安全模型（白名单只在代码里） |
| **`ToolResult` 只有 `contracts` 里那一个类型**（曾有两个同名类型，导致工具结果被静默丢弃） | `winvoice/contracts/messages.py:183` | 工具结果丢失，回复变成假的「好的。」 |
| **工具层目前按 `full` 权限校验**，tier 没有传进来 | `winvoice/tools/executor.py`（见 1.2） | guest 能读写文件、跑脚本 |
| **`logs/*.jsonl` 里没有结构化事件**（只有 httpx 的行） | 见 1.4 | 别指望从文件日志定位问题；只依赖控制台 |
| **不要相信 `process` 日志字段**，它恒等于日志级别 | `winvoice/logging.py:33` | 基于它做统计/告警是错的 |
| **tick 循环吞掉所有异常且不带 traceback** | `pipeline.py:180-182` | 出问题只看到一行 `error="..."`，得靠读代码复现 |
| **测试**：`pytest tests -q`（当前 152 passed, 1 skipped）。加载真实模型的测试要 `skipif` 缺模型；`pytest.ini` 里的 `unit`/`integration` 标记没人用，**按目录选** | `pytest.ini` | `pytest -m unit` 会选中 0 个测试 |

---

## 1. 前置改造（横切，先做这些）

### 1.1 成功结果永远不会被念出来

- **现象**：任何「查询类」需求都答不出来。即便工具成功拿到数据，用户听到的也只是
  「好的，已为您完成。」
- **现状**：`pipeline._default_reply` (`winvoice/audio/pipeline.py:373`) 成功分支是**写死的一句**
  （第 385-386 行），完全不看 `ToolResult.result`。失败分支才会念内容。
- **要做的**：让「成功的工具结果」也能进入 TTS。约定一个面向用户的成功文本，
  优先级与 `message` 一致（例如成功时也允许 `result["message"]`，`_default_reply` 优先念它）。
  这是第 2.1 / 2.2 / 2.3 / 2.4 共同的先决条件。
- **注意**：念出来的内容必须是**短句**——下面的 TTS 是整句合成完才出第一块音频，
  长文本会让用户干等（见 2.1 的延迟说明）。
- **验收**：写一个工具返回 `{"success": True, "message": "…中文…"}`，
  端到端能听到这句话；没有 `message` 时行为与现在完全一致（仍说「好的，已为您完成。」）。

### 1.2 说话人分级没有传到工具层

- **现象**：`guest`（非注册者但分数落在中段的人）照样能读文件、写文件、跑脚本。
- **现状**：`ToolExecutor.execute` 里有一段注释占位 `# Check speaker tier (would be passed
  via context) / For now, assume full tier`（`winvoice/tools/executor.py:53-54`）；
  `registry.validate_call(tool, args, tier)` (`registry.py:219`) **已经实现了** tier 规则，
  只是没人传参；`config.yaml` 的 `tools.guest_denied` **全项目零引用**。
- **要做的**：把 `PipelineContext.sv_result.tier` 一路带到工具调用（建议加到 `ToolCall`
  或 `execute(call, tier=…)`），并把 `guest_denied` 与 `ToolSpec.guest_allowed` 接上；
  然后决定 `tools.guest_denied` 是留用还是从配置里删掉（配置项与实际行为不一致是更坏的状态）。
- **验收**：guest 说话 → 请求 `read_file`/`write_file`/`run_script` 被拒且**用人话**说明
  （中文 `message`）；full 不变；`rejected` 仍在 `_begin_utterance` 就中止。

### 1.3 tick 异常被吞

- **现状**：`pipeline.py:180-182` 的 `except Exception as e: logger.error("pipeline_tick_failed",
  error=str(e), ...)` —— 没有 `exc_info`，也没有 traceback。本文件里若干个 bug（唤醒后哑掉、
  工具结果丢失）当初都是因此被掩盖的。
- **要做的**：至少加 `exc_info=True`；更好的是把异常按 `state` 分类并计数，便于判断是不是
  「同一个错误刷屏」。
- **验收**：人为让某引擎抛异常，控制台/日志里能看到完整堆栈。

### 1.4 结构化日志进不了文件

- **现状**：各模块在 **import 期**就 `get_logger()`，早于 `configure_logging()`；配合
  `cache_logger_on_first_use=True`，这些 logger 冻结了 structlog 的默认配置 →
  事件被渲染到 **stdout**，而 `logs/main.jsonl` 只收到第三方 stdlib 日志（httpx 的请求行）。
  另外 `add_process_name` 的第二个参数是 structlog 传入的**方法名**，不是进程名。
- **要做的**：改成显式 `structlog.stdlib` 绑定（`stdlib.LoggerFactory` + `add_log_level` +
  JSONRenderer，或在 `configure_logging` 后重新绑定），并修正 `add_process_name` 的签名；
  验证 `logs/main.jsonl` 里能看到 `pipeline_started` 这类事件。
- **验收**：单进程跑一次，`logs/main.jsonl` 每行都是合法 JSON 且含 `event`/`level`/`process`/`trace_id`。

---

## 2. 交互功能待办（按用户可感知的缺失排序）

### 2.1 问答 / 闲聊 —— 目前完全不存在

- **现象**：「什么是量子力学」「帮我看看这个项目里有什么」→ 要么被硬塞进某个命令工具，
  要么答「抱歉，这个请求我还没有实现。」
- **现状（已实测）**：`IntentName`（`contracts/messages.py:78`）11 个成员里没有问答；
  `rules.py:114` 的规则全是命令；`pipeline._intent_to_tool_calls` (`pipeline.py:319`) 只映射
  8 个工具 → 无映射 → `_default_reply` 第 383 行返回「没实现」。用真实 3B 模型实测路由：
  `什么是量子力学？` → `get_weather`（参数 `location=量子力学`）；`帮我看看这个项目里有什么`
  → **`search_web`**（→ 弹浏览器，见 2.6）；`你好` → `unknown`。
- **技术细节与建议**：
  - 新增 `IntentName.ASK`；在 `winvoice/llm/local.py` 加一个**自由生成**方法，
    **不要复用 `complete()`** —— 它是意图分类专用：system prompt 写死 "You are an intent
    classifier"、`max_tokens=256`、且带 `INTENT_GRAMMAR` 约束。
  - 答案必须过「可朗读」关：中文、短、无英文单词（第 0 节约束）、无 markdown/列表符号。
    建议硬性截断（例如 ≤ 80 字）并过滤掉拉丁字符与代码符号。
  - **延迟是真实问题**：3B CPU 生成约 10–20 token/s，50 字答案约 3–6 s；而 `TtsEngine.synthesize`
    是**先整句合成再分块 yield**（`tts.py` 的 `generate()` 一次性调用），所以首字延迟 =
    LLM 全文 + 全文合成。要么改成按句流式（边生成边合成），要么先回一句中文垫场
    （「我看一下」）再出答案。
  - 硬约束：问答路径**不得给 LLM 工具能力**（第 0 节），它只是文本进、文本出。
- **验收**：「什么是量子力学」→ 用中文简短作答并念出，**不弹浏览器**；「你好」→ 打招呼；
  LLM 不可用/超时 → 明确说查不到，不静默、不崩。

### 2.2 列目录 —— 「当前目录下有什么文件」

- **现象**：无解。规则层不命中 → LLM 判成 `read_file`，参数是字面量 `"current directory"`
  → `File not found`。
- **现状**：没有 `list_dir`；`read_file`（`builtin.py:361`）只能读文件，且限制在用户目录下。
- **技术细节与建议**：
  - 依赖 1.1（否则列出来了也念不出来）。
  - `ToolName.LIST_DIR` + `ToolSpec`（`required=["path"]`，`destructive=False`）+
    handler：限制在 `Path.home()` 之下（与 `read_file` 同样的 `relative_to` 检查）、
    `os.scandir`、目录优先排序。
  - **必须截断**：输出「一共有 N 项，前面几个是 A、B、C」，否则 TTS 要念几十秒。
  - 规则层加一条明确的触发词（例如「有什么文件 / 列出 / 目录下」），避免又漏给 3B 去猜。
- **验收**：「当前目录下有什么文件」→ 念出数量 + 前几项；空目录、超长目录、
  家目录之外的路径各有合理的中文回应。

### 2.3 查时间

- **现象**：「现在几点了」→ 规则层**能命中** `get_time`（`rules.py` 的 `几点|什么时间|现在时间|time|clock`），但 `_intent_to_tool_calls` 里没有映射 → 「没实现」。
- **技术细节**：`ToolName.GET_TIME` + handler（`datetime.now()`，输出中文说法）；
  零依赖、零网络、最快见效的一条。注意念数字现在没问题（`rule_fsts` 已接）。
- **验收**：「现在几点了」→ 念出当前时间；跨零点不出现「24 点」。

### 2.4 查天气

- **现象**：同上，规则命中 `get_weather` 但没有工具。
- **技术细节与建议**：
  - `httpx` 已是依赖。可用 `https://wttr.in/<city>?format=j1&lang=zh`（无 key）或和风天气（需 key）。
  - 需要在 `config.yaml` 增加城市/开关（以及可能得 API key），并在 `README` 的配置示例同步。
  - 必须 `async`（`httpx.AsyncClient`）+ 超时 ≤5 s，不能阻塞事件循环。
  - 回答要短（当前天气一句话），断网/超时给中文兜底。
- **验收**：有网 → 念出今日天气；断网 → 「暂时查不到天气」；两者都不崩、不静默。

### 2.5 二次确认回路 —— 让 `write_file` / `run_script` 真正可用

- **现象**：任何写文件/跑脚本的请求都回「这个操作需要你先确认，我还没有实现确认的流程。」
  （已修成中文，但功能是死的。）
- **现状**：`__main__._on_tool_call`（`winvoice/__main__.py`）与 `pipeline._run_tools` 都调用
  `executor.execute(call)`，**从不传 `confirmed=True`**；`get_pending_confirmation()` /
  `clear_pending_confirmation()`（`executor.py:131`、`:134`）有 API 无调用者。
  快照那一半是好的（destructive 工具执行前会 snapshot，失败会 restore）。
- **技术细节与建议**：
  - 新增 `PipelineState.CONFIRMING`；`_run_tools` 收到 `CONFIRMATION_REQUIRED` 后：
    念中文问题（`ToolResult.message` 已经具备）→ 等一下一句话 → 判定：
    确认词 = 「确认/是/好/可以/继续」，否认词 = 「取消/不用/算了/不要」。
  - **必须有 TTL**（例如 15 秒或「下一句非确认话即作废」），否则挂起的调用会跨话题误触发。
  - 确认后**重放同一个 `ToolCall`**（`confirmed=True`），不要重新让 LLM 解析一遍。
  - **安全**：挂起的调用要绑定 `trace_id` + 说话人；**guest / 换人不得确认**（依赖 1.2）。
  - 界面提示：说清楚「将对哪个路径做什么」，路径是英文/含中文都可能——念之前要按
    `_speakable` 的规则处理（路径含拉丁字符会被 TTS 丢掉，建议只念文件名或改用「目标文件」）。
- **验收**：「写入文件 X 内容 Y」→ 追问 → 说「确认」→ 真的写入且 `snapshots/` 下有快照；
  说「取消」→ 不写；沉默超时 → 不写；guest 确认 → 拒绝。

### 2.6 打开浏览器的确认制（`search_web`）

- **现象**：任何被误判成搜索的句子都会**立刻弹浏览器**（实测：「帮我看看这个项目里有什么」）。
- **现状**：`builtin.py:343` `search_web` 直接 `webbrowser.open(f"https://www.bing.com/search?q={query}")`
  —— 无确认、无编码（中文直接拼进 URL）。
- **技术细节与建议**：
  - (a) 先用 `urllib.parse.quote` 编码 query；
  - (b) 要么复用 2.5 的确认回路（把 `search_web` 标成 `requires_confirmation=True`），
    要么只在明确说了「搜索/查一下/百度/google」时才允许开浏览器，其余路由到 2.1 的问答。
  - 配置里加个开关会比二选一更实际（例如 `tools.search_web_confirm: true`）。
- **验收**：问句不再弹浏览器；明确说「搜索 X」才开，且 URL 里 query 已正确编码。

### 2.7 唤醒词灵敏度标定（需要用户本人录音，agent 无法独立完成）

- **现状**：引擎侧已验证正常——用模型自带参考音频跑真实 `KwsEngine`（阈值 0.25、100 ms 分块）：
  英文 2/2 触发、中文 5/7 触发。**缺的是用户本人声音的量化数据**。
- **技术细节与建议**：新增 `scripts/calibrate_kws.py`：
  1. 用 `sounddevice` 录 N 遍每个候选唤醒词（16 kHz mono，3 s/遍）+ 一段环境噪声；
  2. 用 `KwsEngine` 在若干阈值（0.10/0.15/0.20/0.25/0.30）下回放；
  3. 输出「命中率 / 误触发率」表，给出建议阈值与 `use_int8` 取值。
  sherpa-onnx **不暴露每次命中的分数**，所以只能这样离线标定。
- **验收**：脚本给出阈值建议表；用建议值重跑，唤醒率可复现；把结论写进 `deployment.md` §5.3。

---

## 3. 基础设施与运维缺口（不直接影响交互，但会拖慢每一次排障）

| 项 | 现状 | 说明 |
|---|---|---|
| 配置热更新 | `ConfigManager.start_watching()` 会重读配置对象，但**没有任何消费者**：引擎只在 `initialize()` 读一次 | 要么做 fan-out（引擎重载），要么在 README/CLI 明确「改配置需重启」，别留半截 |
| Prometheus 指标 | `logging.init_metrics()` **全项目零调用**，`observe_latency`/`inc_request` 是空操作 | 接了才有 KWS 命中率、ASR 延迟这些数据；也是 2.7 的长期替代方案 |
| 磁盘配额 | `storage.max_total_gb` 读进配置但**无任何代码计算/清理** | 模型+日志+快照目前无上限 |
| 模型完整性 | 只有下载路径校验，且 `MANIFEST` 里**只有 KWS 一条钉了真 SHA256**，其余 `sha256: None` | 启动校验（size+mtime → SHA256 → `.corrupt/`）未实现；至少把其余模型的 hash 补上 |
| pytest 标记 | `pytest.ini` 注册了 `unit`/`integration`/`manual`，只有 e2e 用了 `manual` | `-m unit` 选中 0 个；要么给测试打标，要么把标记删掉 |
| `docs/adr/` | `AGENTS.md` 引用了它，目录不存在 | 有了架构决策就建目录并按 `0001-*.md` 命名 |
| 阈值可视化 UI / PySide6 | 全部是 CLI，无 GUI | 阈值目前靠 `--max-inter`/`--min-gap` 调；直方图+滑块属长期项 |
| 全双工 / AEC | 半双工规避回声；打断只到 chunk 边界（≈100 ms），且**只有唤醒词能打断** | 真全双工需要 AEC（WebRTC APM 或 speexdsp），属大改 |

---

## 4. 维护记录

| 日期 | 动作 |
|---|---|
| 2026-09-19 | 建档。条目来源：`spec.md` §15「未实现清单」、`README.md` Known limitations，以及本次调试中实测确认的缺口（路由误判、浏览器弹窗、tier 未传、日志缺陷）。 |

> 删除条目时请**只删条目**，并把同一次提交里同步过的文档（README / deployment / spec）
> 写进提交信息，方便回溯「哪次提交让它从这份文件里消失」。
