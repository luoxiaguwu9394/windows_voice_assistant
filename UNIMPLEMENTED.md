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
| **TTS 只能念中文**。matcha 与 vits 两个中文模型的词表里**拉丁词条为 0** | `winvoice/audio/tts.py`、`models/tts/*/lexicon.txt` | sherpa 逐词丢 `OOV ... Ignore it!`，用户听到一句话中间空掉 |
| **停顿不是模型给的，是我们给的**。词表里**没有任何标点**，sherpa 的 `silence_scale` 实测无效（0.2 与 0.0 输出逐字节相同），且每次 `generate()` 自带 **80–250 ms 的噪声底**（不是数字零，是峰值的 0.78–1.74 %） | `scripts/calibrate_tts_pauses.py` 实测、`winvoice/text/segment.py`、`winvoice/audio/playback.py`、`winvoice/audio/_common.py:trim_edge_silence` | 停顿长度回到「模型随便给多少」：逗号没有停顿、每句多 0.3–0.4 s 死气。改停顿只能改 `tts.pause_*`（播放层写成真实静音帧），不要指望模型或 `silence_scale` |
| **合成是逐段（句/从句）进行的**，段间停顿由 `SpeechSegment.pause_after_ms` 携带 | `winvoice/audio/tts.py:synthesize` | 若把整段回复塞进一次 `generate()`，首字延迟回到「全文合成完」，且块内标点全部失去停顿 |
| **说话长度由 `context.speech_budget` 决定**（工具短句 80、Agent 回答 `tts.reply_max_chars`=240），不再是统一的 80 | `pipeline._run_agent`、`contracts/speech.py` | 改 `MAX_SPEECH_CHARS` 会同时影响工具短句；改 `tts.reply_max_chars` 才是改 Agent 回答长度 |
| **播放只有一个出口**：`winvoice/audio/playback.py` 的 `SpeechPlayer`，整会话只开一次输出流 | 旧的 `__main__._on_tts_chunk` → `AudioStreamManager.play_audio` 就是那个 bug（8 kHz MME 流 + 200 ms blocksize + 无缓冲） | 再往 `sd.OutputStream` 直接写 PCM = 丢缓冲、丢停顿、丢打断 |
| **输出流的两条真机约束**（只有插上设备才暴露，注入假流的单测测不到）：①`sd.OutputStream` **没有 `hostapi` 参数**，要选 host API 只能传**该 host API 下的设备索引**；②**采样率是按路由不同的**：本机同一个扬声器在 WASAPI 下是 **48 000 Hz**、在 MME 下是 **44 100 Hz**，问 WASAPI 要 44 100 会直接 `Invalid sample rate` | `winvoice/audio/playback.py:open_output_stream`、`tests/unit/test_playback_player.py`、`deployment.md` §10 | 一次性解析采样率→首选路由必失败→悄悄退回 MME（老路）；选 host API 用错参数→音频完全打不开（`audio_output_unavailable`） |
| **打断后的重启要防抖**：`abort()` 是事件循环线程发的，可能正撞上喂音线程在 `write()` 里，MME 会回「media data is still playing」拒绝 `start()` | `SpeechPlayer._restart_stream`（重试 0/20/50/100 ms → 仍失败则 `_reopen`）/ `tests/unit/test_playback_player.py` | 不重试 = 一次打断之后**整个会话再也没声音**（且只在真机上复现） |
| **用户可见文本走 `ToolResult.message`，机器文本走 `error`** | `winvoice/contracts/messages.py`（`ToolResult`）、`pipeline.py`（`_speakable` / `_spoken_success`） | 英文错误被念出来 = 上面的 OOV 刷屏 |
| **成功结果现在也会被念**：`_default_reply` 成功分支念 `ToolResult.message`，没有 message 才回「好的，已为您完成。」 | `pipeline.py` 的 `_spoken_success` | 新工具成功了却不给 `message`，用户只听到「好的」= 回到了老 bug |
| **模型词表里 4 条词条的拼音在 `tokens.txt` 里不存在**（Matcha：`谁的`/`谁都`/`人生自古谁无死`/`鹿死谁手` 标了 `shei2`，而 token 表只有 `shui2`） | 实测见 `deployment.md` §10 排障行；`models/tts/matcha-icefall-zh-baker/lexicon.txt` | 控制台每次合成会打一行 `Unknown token: shei2`。**实测无害**：与文本无关（每个 `generate()` 恰好一次），且 `谁的` 的两个音节都念得出来（580 ms ≈ 谁 362 + 的 269）。别去「修」词表 —— 那是上游数据，改了升级就丢 |
| **数字必须靠 `rule_fsts`**（`number.fst`/`date.fst`/`phone.fst`）。引擎已接，模型目录缺这几个文件就会失声 | `winvoice/audio/tts.py` | 「调到90」被念成「调到」 |
| **LLM 永不选工具**（分类器只输出 intent + args，工具选择在代码里）。**2026-09-20 修订**：DSH Agent 模式下，模型**会**选择并调用工具。边界改为 —— 模型只能调用**注册表里已注册**的工具，参数仍过 pydantic 校验、说话人分级、快照与验证器；它仍然**不能**生成并执行任意 shell 命令。工具暴露给 DSH 的唯一通道是 `winvoice/mcp_server.py`，它转手调用同一个 `ToolExecutor` | 原：`winvoice/llm/grammar.py`、`pipeline.py` 的 `_intent_to_tool_calls`；现：`winvoice/mcp_server.py`、`winvoice/tools/executor.py` | 破坏整个安全模型（白名单只在代码里）。<br>**注意**：`dsh.local.profile` 默认 `sdk`，该 profile 自带文件系统等工具 —— 想彻底堵住「模型绕过 ToolExecutor 直接动手」，把 profile 换成 `sdk-minimal`（见 3.x） |
| **`ToolResult` 只有 `contracts` 里那一个类型**（曾有两个同名类型，导致工具结果被静默丢弃） | `winvoice/contracts/messages.py` | 工具结果丢失，回复变成假的「好的。」 |
| **工具层目前按 `full` 权限校验**，tier 没有传进来 | `winvoice/tools/executor.py`（见 1.1） | guest 能读写文件、跑脚本 |
| **`logs/*.jsonl` 里没有结构化事件**（只有 httpx 的行） | 见 1.3 | 别指望从文件日志定位问题；只依赖控制台 |
| **不要相信 `process` 日志字段**，它恒等于日志级别 | `winvoice/logging.py:33` | 基于它做统计/告警是错的 |
| **tick 循环吞掉所有异常且不带 traceback** | `pipeline.py:180-182` | 出问题只看到一行 `error="..."`，得靠读代码复现 |
| **新的工具 handler 可以是 async**：`ToolExecutor.execute` 会 await 返回值（`get_weather` 就是这么做的） | `winvoice/tools/executor.py` 的 `_maybe_await` | 直接 `return` 协程 → 结果是 `<coroutine object>`，回复变成空话 |
| **工具说「成功了」必须真的成功**：`open_app` 原来拿裸名字 `Popen(..., shell=True)` 启动，cmd.exe 报「不是内部或外部命令」而工具照样 `success=True` | `winvoice/tools/builtin.py`（`resolve_app_command` / `_launch`）、`tests/unit/test_app_launch.py` | 用户听到「已经打开谷歌浏览器了」，屏幕上却什么都没有 |
| | **测试**：`pytest tests -q`（当前 344 passed, 1 skipped）。加载真实模型的测试要 `skipif` 缺模型；`pytest.ini` 里的 `unit`/`integration` 标记没人用，**按目录选** | `pytest.ini` | `pytest -m unit` 会选中 0 个测试 |
| **不要用 pytest 的 `tmp_path`**：受限/沙箱环境下它建在系统临时目录里、且被 `chmod 0o700`，写入会被拒（本项目实测 19 个 error 全部来自这里） | `tests/` | 需要临时目录时用仓库内的 scratch（见 `tests/unit/test_dsh_router.py` 的 `work` fixture） |
| **跨进程的「当前这句话」走 `runtime/utterance.json`**：MCP 子进程读它来判定说话人分级 | `winvoice/tools/utterance.py` | 不读它 → guest 能通过 DSH 读写文件（见 1.1） |
| **自适应档案只许 full 教，guest 一票都不许**：guest 判定的音频按定义就不是主人，却曾触发 EMA 把档案向「谁说得最多就向谁漂」——实测 2026-09-27，其他家庭成员被累积漂到 full 分数带；主人注册样本里一个坏样本把 threshold_high 拖到 0.633，进一步压缩区分度 | `sv.verify` 现仅 full 判定自适应；被污染的尾部嵌入已移除（备份 me.json.bak-20260927）；根治靠重录（重算 threshold_high） | 新的核验场景默认 adaptive=False；重录时盯住输出的 threshold HIGH ≥0.70 |
| **声纹核验必须核「说了的话」，不是「房间里最后一段声」；把关不许顺手学习**：①确认应答的 SV 原来跑在滚动窗口上（大半是判停静音+回声）；②更糟的是每次误判 guest 都触发自适应 EMA 把档案往垃圾嵌入拖 5% 并写盘——多次尝试后主人对自己只剩 0.32 分（实测 2026-09-27 完整链条） | 现状：核验用 VAD 段 speech-only 音频（`_current_speaker_tier`）；确认问题播完丢弃 0.4 s 回声窗口；`sv.verify(adaptive=False)` 让把关不修改档案；rejected 自动重问一次；确认短语改「确认执行」 | 任何把关型核验都必须传 adaptive=False；回声窗口内不得喂 VAD；滚动窗口只适合说话中触发 |
| **同一条口语路径在每个环节必须解析成同一个文件**：「桌面\新建.txt」若 handler 做了文件夹词映射而验证器/快照不做，写入成功却被判 failed（还会误升级云端）；若谁都不做，就写进仓库里名叫「桌面」的文件夹 | 实测 2026-09-27；`builtin.resolve_user_path` 是唯一入口，handler/验证器/快照/registry 校验全走它 | 新增文件类工具或新文件夹别名时，四处只有一处接上就会复发 |
| **MCP 工具 schema 的 anyOf 子模式必须是完整对象**：llama.cpp 的 json-schema 转换器对裸 `{"required":[...]}` 回 `400 Unrecognized schema`——`set_volume` 的二选一参数曾以这种形式渲染，**每一个**带工具的 DSH 本地回合都 400（`dsh_resolved_locally` 自 DSH 上线起计数为 0，2026-09-27 才首次出现） | `mcp_server.to_json_schema` 的 anyOf 分支现携带完整 type/properties/required；修复后本地回合首次 resolved | 往 MCP 暴露的任何 schema 都要用 llama-server 实测（`tests/integration/test_mcp_server.py` 只查结构不查转换） |
| **启动命令 ≠ 进程映像**：`ALLOWED_APPS["vscode"]="code"` 是经 shim 解析的启动命令，真实进程是 `Code.exe`；拿启动命令当进程名用，`close_app`/验证器都会说「没有进程可关」（VS Code 能开不能关的实测根因，2026-09-27） | `builtin.APP_PROCESS_IMAGES` 把两者分开，`close_app` 与 `_expected_image` 都查它 | 下一个「经 shim/别名启动」的应用（如 Insiders 版、winget 别名）再进白名单就会复发，加表不加名 |
| **控制台子系统程序不给新控制台就会借走助手的**：`Popen` 启动 cmd.exe/powershell.exe 若不带 `CREATE_NEW_CONSOLE`，子进程继承调用方控制台——横幅打进助手窗口、stdin 被共享，用户看到「原窗口刷新了一下」而没有新窗口 | 实测 2026-09-27（日志里紧跟 `tool_call` 的两行 cmd 横幅）；`builtin._launch` 现读 PE 头 `Subsystem`（CUI=3）决定加旗标，GUI 程序不受影响 | 只修名单（cmd/powershell 加旗标）= 下一个进白名单的控制台程序再踩一遍 |
| **`explorer.exe` 是 Windows 外壳，不是「一个应用」**：桌面、任务栏、开始菜单、文件夹窗口全是它。`taskkill /f /im explorer.exe` 会干掉整个 GUI —— 2026-09-20 实测：用户说「关闭文件资源管理器」，桌面直接消失 | `winvoice/tools/builtin.py`（explorer 分支 + `PROTECTED_PROCESSES`）、`winvoice/tools/_explorer.py`、`tests/unit/test_close_app_safety.py` | 「关文件夹窗口」变成「关掉整个桌面」。而且**验证器还会判它成功**，因为后置条件被写成了「explorer.exe 不存在」 |
| **默认优雅关闭；`/f` 必须由用户显式要求**：`taskkill /f` 跳过应用自己的「是否保存？」提示 | `builtin.close_app` 的 `force` 参数、`intent/rules.py` 的强制词识别 | 「关闭记事本」会静默丢掉未保存的内容，用户毫无机会拦 |
| **验证器只能核对「你叫它核对的那个条件」**：后置条件写错，它就会为错误的目标准确盖章 | `winvoice/tools/verifier.py`：explorer 改查「文件夹窗口数 = 0」，而不是「进程是否消失」 | 把「桌面没了」判为「操作成功」 |

---

## 1. 前置改造（横切，先做这些）

### 1.1 说话人分级没有传到工具层 —— ✅ 已实现，条目下线

（2026-09-20 下线。做法与验收见下。）

- **做过的**：`ToolCall` 新增 `tier` 字段；`ToolExecutor.execute` 把它传给
  `registry.validate_call(tool, args, tier=…)`（该函数一直支持 tier，只是没人传参）；
  `AudioPipeline._intent_to_tool_calls` 不再需要自己过滤。
  `tools.guest_denied` 保留在配置里，因为它现在真的生效了。
- **为什么必须同时改**：DSH 集成后工具会在 **DSH 自己拉起的子进程**里执行
  （`python -m winvoice.mcp_server`），一个 Python 参数跨不过进程边界，所以
  音频进程把「当前这句话的说话人分级」发布到
  `winvoice/tools/utterance.py` 的 `runtime/utterance.json`，MCP 服务器每次调用前读它。
  不这么做，DSH 就不是权限模型的使用者，而是绕过它的通道。
- **验收**：`tests/unit/test_executor_verification.py`（guest 读文件/跑脚本被拒且回中文；
  guest 仍可 `get_time`；rejected 一律拒绝；无 tier 的旧调用仍是 full）
  + `tests/integration/test_mcp_server.py`（同一个 read_file 调用，guest 被拒、full 正常）。
- **仍未做**：敏感应用名单（guest 现在仍能打开任何白名单应用）——
  见第 3 节「敏感应用名单」。

### 1.2 tick 异常只补了 traceback，分类与计数仍未做 —— 🔶 部分下线

（2026-09-26 部分完成：`run()` 的 `pipeline_tick_failed` 现在带 `exc_info` 与 `error_type`；
新增的回合任务也有 `turn_failed`（含堆栈），不再让后台任务的异常变成一句
「Task exception was never retrieved」。见 `tests/integration/test_pipeline_barge_in.py`。）

- **仍未做**：按 `state` 分类并计数，用来判断是不是「同一个错误刷屏」。现状是一行一个事件，
  但不聚合。
- **验收（剩余部分）**：同一 state 下连续失败能看出是同一个错误（计数 + 首次/最近时间戳）。

### 1.3 结构化日志进不了文件

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

### 2.0 Agent 思考 / 播报时无法打断 —— ✅ 已实现，条目下线

（2026-09-26 下线。做法与验收见下。）

- **做过的**：回合改成 `asyncio.Task`（`AudioPipeline._start_turn` / `_run_turn`），
  新增 `PipelineState.AGENT_THINKING`，`_tick` 在 `AGENT_THINKING` 与 `TTS_PLAYING` 下继续喂 KWS；
  打断时 `tts.interrupt()` + `speech_player.interrupt()`（`abort()` 立刻丢弃设备缓冲）
  并 `_turn_generation += 1`，被作废的回合**不再播报**（`turn_superseded`）。
  按原建议**不取消** DSH 回合 —— 取消会把 harness 留在未知状态，而丢弃输出已经解决了问题。
- **验收**：`tests/integration/test_pipeline_barge_in.py` —— 播报中唤醒词 → 播放器被打断且回合被作废、
  新一句话立刻进入聆听；Agent 思考中唤醒词 → 回合输出不入队、队列长度有界、状态回到聆听；
  另有「回合运行中 tick 循环仍在消费音频」的回归测试。原 `_route_intent` 保留为同步入口（测试与
  逐步驱动仍可用），实时路径走 `_start_turn`。

### 2.1 问答 / 闲聊 —— ✅ 已实现，条目下线

（2026-09-27 下线。做法：规则层新增 `IntentName.ASK`（问句/问候触发词排在具体话题之后、
SEARCH_WEB 弱动词之前），pipeline 在 `_run_turn` 里**先于 agent 拦截** ASK；
`LocalLlmBackend.generate` 自由生成（不复用分类的 `complete()`），答案过 `sanitize_for_tts`
（≤ `llm.ask.max_chars`=80、拉丁词/markdown 全剥）后播报；`你好/谢谢/再见` 等寒暄由规则层
直接给中文回话，不经模型。LLM 不可用/超时/答案不可念 → 明说「答不上来」，绝不回落给 agent
（问答路径无工具能力）。验收：`tests/unit/test_intent_rules_qa.py`、
`tests/integration/test_pipeline_qa.py`。新缺口见 §3「问答依赖 llama-server 在跑」。）

### 2.2 列目录 —— ✅ 已实现，条目下线

（2026-09-27 下线。做法：`ToolName.LIST_DIR` + handler（与 `read_file` 同样的 `relative_to(home)`
限制；无 path 参数默认列用户目录；目录优先排序；**只念纯中文名**，拉丁名计数但不念，超过 4 个
念「还有其他」）；guest 不可用（同 `read_file`）；规则层触发词
`列出/什么文件/目录下/文件夹里/看看目录`，排在 ASK 之前。验收：`tests/unit/test_list_dir.py`。）

### 2.3 二次确认回路 —— ✅ 已实现，条目下线

（2026-09-27 下线。做法：新增 `PipelineState.CONFIRMING`；`_run_tools` 收到
`CONFIRMATION_REQUIRED` 后挂起 `_PendingConfirmation`（原 `ToolCall` + tier/trace 绑定 +
`tools.confirm_timeout_s`=15 s 截止时间）、念中文问题（不念拉丁路径）并进 CONFIRMING——该状态下
麦克风**免唤醒词**直接喂 VAD/ASR；「取消」词优先于「确认」词（「不要确认」不会误确认）；
确认后**重放同一个 `ToolCall`**（`confirmed=True`），不让 LLM 重解析；下一句非确认话 = 作废挂起
并按全新话语重路由（新 trace、重新声纹校验、被拒声纹直接终止）；TTL 到期作废；任何
`_abort_utterance` 都会丢弃挂起调用。**guest/换人不能确认**（比较挂起 tier 与当前 SV tier，
日志 `confirmation_speaker_mismatch`）。快照链路不变。验收：
`tests/integration/test_pipeline_confirmation.py`。）

### 2.4 打开浏览器的确认制（`search_web`）—— ✅ 已实现，条目下线

（2026-09-27 下线。做法：复用 2.3 的确认回路——`_run_tools` 在执行 SEARCH_WEB 前若
`tools.search_web_confirm`（默认 true，热生效）则先挂起、念「你要我搜索 X 吗？确认请说确认，
取消请说取消」（query 含拉丁字母时只说「打开浏览器搜索吗」）；确认后照常执行。**刻意只作用于
语音路径**：agent 经 MCP 调用的 `search_web` 不受影响，避免把 agent 的搜索变成死路。验收：
`tests/integration/test_pipeline_confirmation.py`。）

### 2.5 唤醒词灵敏度标定（需要用户本人录音，agent 无法独立完成）

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
| **DSH 自带的工具没被限制** | `dsh.local.profile` 默认 `sdk`，该 profile 除 MCP 桥接外还带 DSH 自己的文件系统/命令工具。也就是说 agent **可以绕过 `ToolExecutor` 直接动手**，尽管它同时也有 `mcp__winvoice__*` | 要彻底贯彻「No model should be able to bypass these boundaries」，应把 profile 换成 `sdk-minimal`（只保留 shell、本地执行、JSONL 会话，不挂文件系统工具），或用 patch 层 disable 掉 DSH 自己的 fs 工具行。当前未做：`sdk-minimal` 是否保留 JSON-RPC server 行尚未实测，贸然切换有启动失败风险 |
| **升级条件比 new_way.md 窄** | `assess_turn` 只在**验证器判定 failed**、回合异常结束、回答为空时升级；`new_way.md` 还列了「Tool execution fails and the local model cannot recover」，设计稿 §3.4 写的是 `success == False + retryable == False` | 这是**有意收窄**（见 `winvoice/tools/verifier.py:unresolved` 与 `winvoice/dsh/validation.py` 的 docstring）：没有验证结论的失败是**确定性拒绝**（不在白名单、确认回路未实现），云端会被同样拒绝，升级只换来一次无用的往返。若将来要严格对齐 new_way，需要区分「值得重试的工具失败」与「策略拒绝」——目前没有可靠信号 |
| **升级上下文缺工具参数** | `ToolActivity.name` 多数情况为空（只解析工具**结果**，不解析 `tool/call` 事件），也没有 args | 云端因此看到 `tool=unknown`。够驱动升级决策（决策只看验证结论），但不足以让云端「接着上次做」。补齐需要解析 DSH 的调用事件，而它是 pre-release，信封可能变 |
| **敏感应用名单** | 分级已经生效（见 1.1），但 `open_app`/`close_app` 对 guest 与 full **一视同仁**：白名单里 9 个应用 guest 都能开 | 需要一份「敏感应用」清单（设置、cmd、powershell 至少应算），并接到 `ToolRegistry.get_allowed(tier)` / `validate_call` 上。清单来源应当是配置（`tools.sensitive_apps`），不要写死在代码里 |
| **验证器只覆盖 5/11 个工具** | `get_time`/`get_weather`/`read_file`/`list_dir`/`search_web`/`media_control` 没有验证器 | 前四个确实没有可观测的后置条件（查询类、只读）。`media_control` 与 `search_web` 是「能验证动作、不能验证目标」：要验证目标需要 UI Automation / 读取媒体会话，属大改。当前它们既不升级也不阻断，是诚实的选择 |

| **问答依赖 llama-server —— 🔶 部分下线（2026-09-27，llama-server 自动托管）** | `winvoice/llm/server.py` 现在托管服务生命周期：启动时先探测 `/health`，已在跑就复用（关闭时也不动它），没在跑按 `llm.local.server_binary/model_file` 自动拉起并等健康检查（输出进 `logs/llama-server.log`）；拉起失败只是警告，助手照常启动。**「忘了开终端 1 导致问答/分类/DSH 全静默降级」已消除** | 剩余：若接受「问答可以让 agent 回、只是不许它动工具」，需要 DSH 支持按回合挂起工具（或单独的 no-tools profile），当前没有这个开关。寒暄（你好/谢谢/再见）不经模型，不受影响 |
| **确认回路只覆盖语音路径（2026-09-27 新增，来自 2.3/2.4）** | DSH 经 MCP 调 `write_file`/`run_script` 仍是确定性拒绝（MCP 子进程无法与用户对话确认）——实测：本地 agent 对「桌面建 txt」会如实回「需要先确认」，文件不会被创建；语音路径说「写入文件…」→「确认」即可建成。`search_web` 则直接执行 | 让 agent 侧也走确认需要把「挂起 + 问句」跨进程回传给音频进程，并让 harness 学会等一轮用户输入——等 DSH 的交互模型稳定后再评估。另：CONFIRMING 状态不喂 KWS（唤醒词会被当文本转写、走「非确认话=作废并重路由」，行为正确但多花一次 ASR） |
| **DSH 回合的 `tool/call` 事件未解析** | `winvoice/dsh/validation.py` 只认**工具结果**（按本项目自己写的 `success` 载荷识别），不解析调用事件 | 因此 `ToolActivity.name` 在多数情况下为空，升级上下文里只能写 `tool=unknown`。已经足够驱动升级决策（决策只看验证结论），但想让云端 agent 看到「本地调了哪个工具」需要再解析一层；风险是 DSH 是 pre-release，事件信封可能变 |
| 配置热更新 | `ConfigManager.start_watching()` 会重读配置对象，但**消费者只有两个**：`get_weather`（每次调用重读）与语音路径的 `speech_text_config()`（每个回合重读分块/停顿表，2026-09-26 起）。引擎仍在 `initialize()` 只读一次 | 分块与停顿表可以热调（见 `HOT_RELOADABLE`），模型/采样率/输出设备不行（列在 `REQUIRES_RESTART`）。别再指望其余字段会热生效 |
| Prometheus 指标 | `logging.init_metrics()` **全项目零调用**，`observe_latency`/`inc_request` 是空操作 | 接了才有 KWS 命中率、ASR 延迟这些数据；也是 2.5 的长期替代方案 |
| 磁盘配额 | `storage.max_total_gb` 读进配置但**无任何代码计算/清理** | 模型+日志+快照目前无上限 |
| 模型完整性 —— 🔶 部分下线（2026-09-27，本地封存校验） | `winvoice/integrity.py` + `download_models.py --seal`：把 `models/` 现状（SHA256+size，排除声纹档案与 KWS 关键词缓存这类运行时数据）封存进 `models/integrity.json`；启动做 size 快查、`--check` 全量哈希并列出失败（退出码 1），**只报告不删除** | 剩余：封存是 trust-on-first-use，抓不住「下载时就是坏的」——MANIFEST 其余 10 条的官方归档哈希仍需重新下载才能补 |
| pytest 标记 | `pytest.ini` 注册了 `unit`/`integration`/`manual`，只有 e2e 用了 `manual` | `-m unit` 选中 0 个；要么给测试打标，要么把标记删掉 |
| `docs/adr/` | `AGENTS.md` 引用了它，目录不存在 | 有了架构决策就建目录并按 `0001-*.md` 命名 |
| 阈值可视化 UI / PySide6 | 全部是 CLI，无 GUI | 阈值目前靠 `--max-inter`/`--min-gap` 调；直方图+滑块属长期项 |
| 全双工 / AEC | 半双工规避回声；**2026-09-26 起**打断是 `stream.abort()`（立刻丢弃设备缓冲），不再等 chunk 边界。仍**只有唤醒词**能打断：KWS 没有别的触发词，也没有 VAD 级打断 | 真全双工需要 AEC（WebRTC APM 或 speexdsp），属大改 |
| **访客音色只靠语速**（matcha zh-baker 是单说话人） | `TtsEngine._speed_for`：`num_speakers <= 1` 时访客用 `tts.guest_speed`（默认 1.0，绝对值；主人默认 0.9，两者差 11 %）；多说话人模型仍按 `guest_speaker_id` 换音色 | 单说话人模型下主人与访客音色相同，只有语速差异。想要真正两种音色就换多说话人模型（如 `vits-zh-hf-fanchen-C`，16 kHz，187 说话人） |
| **LLM 侧不流式** | `DSHRouter.route()` 一次性返回全文，`TtsEngine.synthesize()` 只能等拿到整段文本再切句 | 首字延迟 ≈ 首块合成（约 0.3 s）+ 模型出文时间。要边生成边播需要 DSH 的增量事件 |
| 天气数据源单一 | `get_weather` 只用 `https://wttr.in`：**无 key**，但会限流，而且**不返回中文**（`weatherDesc`/`lang_zh` 恒为英文，2026-09-19 实测），所以中文靠 `winvoice/tools/weather.py` 里的 WWO 码对照表 | 换和风天气等需 key 的源可拿到官方中文与更稳的 SLA；要动的前提是把 key 放进 config（`${VAR}` 展开已支持）并保留中文兜底。缺 code 时当前**静默丢描述**，加日志/上报会更好排查 |
| 应用启动只证明「系统接受了启动请求」 | `open_app` 现在按真实路径启动（`App Paths` → `PATH`），进程创建失败会如实报错；但 Chrome 已有实例时主进程会立刻退出，所以只能报「已发出启动请求」 | 想真正确认「窗口起来了」需要 UI Automation / `EnumWindows` 之类的手段，属大改；当前至少不再出现「cmd 报错但工具说成功」 |
| 免安装 / 绿色版程序打不开 | 解析只查 `PATH` 与 `App Paths` 注册表，没有第二套「安装目录猜测表」（那样的表一定会腐烂） | 用户把目录加进 `PATH` 即可；若将来常见，可考虑读取 `HKCU\...\App Paths` 之外的注册来源或允许 `config.yaml` 里手工登记路径 |
| **安装器未签名 + 无正式发布渠道**（2026-09-27 向导上线后新增） | `WinVoice-Setup-*.exe` 是未签名 PyInstaller onefile：SmartScreen 首拦、部分杀软误报；GitHub Releases 还没发过首个资产，而更新检查依赖资产名 `WinVoice-Setup-<版本>.exe` | 首次发版把 exe 按这个名字传到 GitHub Releases；签名证书到位前，误报严重时的备选是 onedir+zip 分发（架构不变，见 `installer/README.md`） |
| **静默自动更新**（2026-09-27 向导上线后新增） | 更新是「提示 → 下载新安装器 → 引导重跑」，无后台静默升级；模型也只在安装时下载 | 要静默化需要把运行时与模型目录拆开做原子替换 + 版本化回滚；向导侧已具备版本探测（`wizard/core/update.py`），只差执行器 |
| **离线模型大包**（2026-09-27 向导上线后新增） | 向导只支持在线下载；无网/弱网机器装不了 | 可做一份 models 离线 zip + 向导「从本地导入」页（`download_models.py` 的 `--models-dir` 已天然支持指过去） |

---

## 4. 维护记录

| 日期 | 动作 |
|---|---|
| 2026-09-27 | **分发上线：一键安装向导（用户需求：「一个 exe，wizard 解决所有 deployment 问题；要考虑更新；唤醒词可设置」）。** `installer/` 新增 `build_runtime.py`（内嵌 Python 3.12 + 钉版依赖 + llama.cpp b7376 + DSH .pylibs，xz 载荷 658MB→143MB）与 `build_installer.py`（PyInstaller onefile，产物 `WinVoice-Setup-0.1.0-dev.exe` 157MB，版本戳写入 gitignore 的 `_version.py`）；`installer/wizard/` 分 core（纯逻辑，45 项单测）与 tkinter UI（11 页）。应用本体**零改动**：安装目录复刻仓库布局 + CWD=安装根，CWD/`__file__` 两条路径解析链原样成立（决策记录 `docs/adr/0001`，spec §12 的 Packaging deferred 就此落定）。配套改动：`download_models.py` 增 `--only`、`--progress-fmt machine`（MODEL/PROGRESS/RESULT 协议行）与 `WINVOICE_HF_ENDPOINT` 镜像；新脚本 `scripts/audio_probe.py`（设备枚举/播放实测/录音电平，走真实播放路由）；`config/config.template.yaml` 模板（`@TOKEN@` 行级渲染，注释保留）；`install_dsh_bridge.py` 增**捆绑运行时回退**（`dsh` CLI = `deepseek_harness_runtime:main`，不再依赖 PATH/npm，实测桥接安装成功）；`requirements.txt`/`pyproject.toml` 补 `sentencepiece`/`pypinyin`（KWS 实需但一直缺失）。**唤醒词向导可设**（校验 2–12 字、≤8 个，写入 `kws.keywords`，缓存按内容自动重建）；**更新**：安装标记记版本，向导启动查 GitHub Release 新资产→提示下载→引导重跑，升级=重解压载荷（载荷不含 models/config/logs/runtime，用户数据天然保留）。验收：全量 pytest 694 passed（新增 45 项向导 core 单测）；无头 E2E 对真实载荷跑通全管线（解压→默认配置→模型幂等→配置渲染→DSH 桥接→`--check` 通过→标记）；exe 已产出，GUI 点选流程待用户真机验收。新增 §3 三条缺口（签名与发布渠道、静默更新、离线大包）。 |
| 2026-09-27 | **声纹安全修复：其他人被识别成 full。** 根因二连：①`sv.verify` 在 **guest 档也触发自适应更新**——guest 判定的音频按定义不是主人，但每次都把档案最新嵌入向说话者 EMA 漂移 5% 并写盘，家里常客的声音被逐步漂进 full 分数带（confirm 路径的 adaptive=False 只堵了半个口子，唤醒路径仍在漏）；②`threshold_high=0.633` 由注册时最差一对样本（min_intra≈0.683）决定——一个坏注册样本把主人门槛拖到与同性别 impostor 常见相似度（0.55–0.65）重叠的位置。修复：①guest 判定**一票都不许**改档案（仅 full 自适应）；②移除档案中唯一被反复 EMA 改写的尾部嵌入（备份 `me.json.bak-20260927`；剩余 7 条为原始注册样本，主人 max 打分不受影响、impostor 的友好方向被移除）；③deployment 排障行给出重录指引（threshold HIGH ≥0.70 为健康线）。§0 约束行更新；新增 2 项回归（guest 判定不教档案/full 判定保留教学；全套 649 passed, 1 skipped）。 |
| 2026-09-27 | **确认声纹二轮：档案污染实锤 + 四层修复。** 用户复测（分数 0.3229/rejected）证明窗口修复不够——追出完整根因链：①滚动窗口（判停静音+回声为主）→ 嵌入退化；②每次误判 guest 触发 `_adaptive_update` 把 `me.json` 最新嵌入向垃圾音频 EMA 漂移 5% 并立即写盘（档案修改时间即证据）→ 多次尝试后污染累积，干净语音只剩 0.32 分。修复：①`SvEngine.verify` 增 `adaptive` 参数，**把关型核验（确认/重路由）传 False——gate 时不许学习**，唤醒路径保留漂移跟踪；②确认问题播完丢弃 0.4 s 回声衰减期（`_CONFIRM_ECHO_GUARD_S`）再喂 VAD，杜绝问题尾音混进应答段；③核验 `rejected`/无法判定时**保留挂起请求自动重问一次**（第二次才作废；guest 重试同样失败，不放大权限）；④确认短语改「确认执行」（四音节嵌入远稳于两音节），确认词表天然匹配。新增 4 项回归（rejected 保留重试/二次拒绝作废/把关不学习/回声窗口忽略；全套 647 passed, 1 skipped）。§0 约束行更新；deployment 排障行更新（含重录声纹指引）。 |
| 2026-09-27 | **实测修复：full 主人确认写入时每次被判 guest。** 根因：确认应答的声纹核验 `_current_speaker_tier()` 跑在麦克风**滚动窗口**（`_recent`，约 1 s）上——短促的「确认」结束 VAD 段后，窗口内大半是判停所需的 ≥500 ms 静音（还可能混有确认问题的扬声器回声），CAM++ 嵌入退化、余弦分数从 full 带（≥0.60）掉进 guest 带（0.40–0.60），`confirmation_speaker_mismatch` 每次触发、确认永远被拒。唤醒路径不受影响（触发时窗口全是连续说话），所以只有确认环节暴露。修复：确认与话题重路由的声纹核验改用 **VAD 段的 speech-only 音频**（与被转写内容同源，`SvEngine.verify` 原生支持 ndarray）；滚动窗口保留给唤醒路径。新增 2 项回归（核验对象必须是 VAD 段、speech-only 分级不再降级主人；全套 643 passed, 1 skipped）。§0 新增约束一行；同步 deployment 排障表。 |
| 2026-09-27 | **实测修复：「帮我在桌面建立一个TxT文件」建不成。** 用户实测（日志）：规则层未命中（「建立」不在 创建/新建/写入/保存 词表），**本地 3B 分类器正确判成 write_file 并提取路径「桌面\新建.txt」**，但三处断点连环：①`content` 必填 → `Missing required argument: content` 被拒（且念的是权限话术「这个操作我暂时不能替你做」，答非所问）；②「桌面」无映射——按 CWD 解析会写进仓库里名为「桌面」的目录（confinement 因仓库在 home 下而放行）；③若走到 agent 路径同样死路（写文件需口头确认，DSH 子进程无法代确认）。修复：`write_file.content` 改可选（无内容=建空文件，即 Windows 新建文本文档语义；**已存在且未说内容 → 拒绝不覆盖**，确认问题无法区分「新建」与「清空」，所以工具向用户要内容）；新增 `builtin.resolve_user_path` 统一口语文件夹词映射（桌面/下载/文档/图片/音乐/视频，handler、registry 校验、WriteFileVerifier、快照模板四处共用——修复过程中发现验证器与快照仍用原始路径，成功的写入被判 failed 并可能误升级，已同源）；schema 校验失败与权限拒绝分离话术（「我没听清这个操作的具体要求」vs tier 拒绝）；路由回退：规则匹配到 write_file 但提取不到参数时让分类器填（「写入文件 X」不再是死胡同）。真机链路：分类器原参数 → 校验通过 → 确认 → 真实桌面空文件 → 验证器 verified → 再确认一次触发防覆盖拒绝+快照回滚。新增 `tests/unit/test_write_file_flow.py`（14 项）；§0 新增约束一行（全套 641 passed, 1 skipped）。同步 README / deployment 排障表。 |
| 2026-09-27 | **实测修复：本地 DSH agent 从未成功过（每个回合 400）；云端通路核验。** 用户问「本地模型能否经 DSH 调工具（桌面建 txt）+ 云端通路是否完好」。排查实锤：①**本地**——llama-server 日志显示每个带工具请求都 `400 JSON schema conversion failed: Unrecognized schema: {"required":["level"]}`：`mcp_server.to_json_schema` 把 `at_least_one` 渲染成裸 `required` 子模式，llama.cpp b7376 转换器不认，而工具列表随每个请求下发 → 本地 agent 自上线起 0 次成功（日志 `dsh_resolved_locally` 计数 0，非本次回归）。修复：anyOf 分支改为完整对象模式（type/properties/required/additionalProperties），真机 200；重跑「桌面建 txt」→ **首次 `dsh_resolved_locally`**：agent 正确调用 `write_file`、被确认闸拦下后如实答复、文件未创建。语音路径验证：未确认拒绝 → 说「确认」后写入桌面成功且验证器 verified。②**云端**——通路结构完好（升级正确触发、适配器/路由正常），但 `MISSING_CREDENTIAL`：DEEPSEEK_API_KEY 既不在用户/机器环境变量、DSH 凭证库里也只有本地占位键——**这台机器从未持久化过云端密钥**（2026-09-20 的验证应为临时 export）。修法：`setx DEEPSEEK_API_KEY` 后重启助手，或经 DSH Models 页写入凭证库；deployment 排障表加行。§0 新增约束一行；`tests/integration/test_mcp_server.py` 断言更新（全套 627 passed, 1 skipped）。 |
| 2026-09-27 | **本地大模型闭环（用户选定范围）：llama-server 自动托管 + 模型封存校验。** ①**llama-server 托管**（`winvoice/llm/server.py`）：部署从「先手动开终端 1」变成只跑 `python -m winvoice`——启动先探测 `/health`，已在跑就**复用且绝不代杀**，没在跑按 `llm.local.server_binary/model_file` 自动拉起（输出进 `logs/llama-server.log`、无窗口、CREATE_NO_WINDOW），等健康最长 `start_timeout_s`=60s；失败只警告不致命（规则/寒暄照常，问答明说答不上来）；`shutdown()` 只 terminate 自己拉起的子进程；stub 模式不碰服务。新配置 `llm.local.auto_start/server_binary/model_file/server_context/start_timeout_s/server_args`（均 REQUIRES_RESTART）。**意图分类(Tier 2)、问答(ASK)、DSH 本地 agent 共用的这条链路从此不再因「忘了起服务」静默降级**。②**模型封存校验**（`winvoice/integrity.py` + `download_models.py --seal`）：本机现状指纹进 `models/integrity.json`（101 个文件，排除声纹档案/关键词缓存），启动 size 快查、`--check` 全量哈希并指名损坏文件（退出码 1），只报告不删除；支持 `--seal --model <key>` 单模型重封、保留其余封存记录。真机验收：不开终端 1 → `llama_server_started`（1.4 s 就绪）；手动起服务后 `--check` → `llama_server_reused` 且检查结束后服务仍在；问答真实生成「量子力学是研究微观粒子行为的物理学分支。」；`--check` 全 [OK] 退出码 0。新增 `tests/unit/test_llama_server_manager.py`（12 项）、`tests/unit/test_integrity.py`（10 项）；§3 两条改写为部分下线（全套 627 passed, 1 skipped）。同步 README / deployment（§5.1、§6 终端 1 可选、排障两行）/ spec（§7.1、§9.2）。 |
| 2026-09-27 | **实测报障修复：VS Code「能打开、关不掉」。** 根因：`ALLOWED_APPS["vscode"]="code"` 存的是启动命令（经 App Paths `Code.exe` / PATH `code.cmd` shim 解析，所以打开正常），而 `close_app` 与 `_expected_image` 把它当进程名——`"code"` 不以 `.exe` 结尾，直接进「我关不掉代码编辑器。」拒绝分支，`taskkill` 根本没构建。修复：新增 `builtin.APP_PROCESS_IMAGES`（启动命令与进程映像分表，`vscode` → `Code.exe`），`close_app` 的 `taskkill /im` 与验证器的 `process_absent/process_running` 都改查映像表；顺带 VS Code 的**打开**验证从 NOT_VERIFIABLE 变为可验证。新增 5 项回归（`tests/unit/test_close_app_safety.py`）；§0 加约束一行（全套 605 passed, 1 skipped）。同步 deployment 排障表。 |
| 2026-09-27 | **新增电源操作 `system_power`（用户需求：「让它关机之类的命令行操作」）。** `ToolName/IntentName.SYSTEM_POWER` + 规则层触发词（关机/重启/重新启动/睡眠/休眠/锁屏/锁定屏幕/注销/退出登录，排在 ASK 之后——「怎么关机」是问句；OPEN_APP 之前——「重新启动」不能被「启动」抢走），触发词与动作共用一张 `_POWER_ACTIONS` 表。handler 用 `Popen` fire-and-forget（`subprocess.run` 会把音频循环挂到机器唤醒为止），关机/重启带 `shutdown /t 5` 缓冲（`shutdown /a` 可中止）。三道闸沿用既有机制：registry `requires_confirmation=True`（语音确认回路问「我将要关机，确认请说确认…」，动作名取自 handler 同一张 `POWER_ACTION_SPEECH` 表）、`guest_allowed=False`、agent 经 MCP 拿不到确认（CONFIRMATION_REQUIRED 是确定性拒绝）——任何模型层都无法自行关机。无验证器（fire-and-forget 无可观测后置条件，与 media_control 同类）。新增 `tests/unit/test_system_power.py`（17 项）；`test_core`/`test_mcp_server` 枚举补齐（全套 600 passed, 1 skipped）。同步 README / deployment / spec。 |
| 2026-09-27 | **实测报障修复：「打开命令提示符」不弹新窗口。** 日志里 `tool_call` 后紧跟着两行 cmd 横幅——控制台子系统程序 `Popen` 启动时**继承调用方控制台**：横幅打进助手窗口、stdin 共享，这就是用户看到的「原窗口刷新了一下」。修复：`builtin._launch` 读目标 PE 头的 `Subsystem` 字段（`IMAGE_SUBSYSTEM_WINDOWS_CUI=3`，免维护名单），控制台程序加 `CREATE_NEW_CONSOLE` 弹独立窗口；GUI（记事本/Chrome/…）与 `.cmd/.bat` 脚本路径不变。真机验证：open_app 产生新 cmd 进程与独立窗口（验证后已单独关闭）。新增 6 项回归（`tests/unit/test_app_launch.py`，含真实二进制的子系统探测；全套 583 passed, 1 skipped）。新增 §0 约束一行；同步 README / deployment 排障表。 |
| 2026-09-27 | **实测报障修复：问「明天佛山天气怎么样」回了「北京今天」。** 两个叠加根因：①规则层城市提取的 4 字贪婪窗口在口语「明天佛山**的**天气怎么样」里错位——组内吞不下「的」，窗口后移一位，提取出「天佛山」，wttr.in 对它 500，工具按设计静默回退 `weather.city`（北京）；②「明天」从未被提取，工具永远答 `weather[0]`（今天）。修复：`_WEATHER_CITY_PATTERN` 把「的」移到窗口外（`(?:的)?`，拉丁名同样受益：「Shanghai的天气」）；新增 `_WEATHER_DAY_OFFSETS`（明天/明晚→1，后天/大后天→2），`get_weather` 增 `day` 参数按条目取预报，`WeatherSummary.day_index` 决定播报的今天/明天/后天标签——payload 条目不足时按**实际**条目如实说（要明天、只有今天 → 说「今天」），明天/后天句不含「现在 X 度」（那是对今天的断言）；城市回退保留 day。真机链路验证：「明天佛山的天气怎么样」→「佛山明天晴，气温 27 到 35 度。」新增 9 项回归（`tests/unit/test_weather_speech.py`，全套 578 passed, 1 skipped）。同步 README / deployment（§8.5、排障表）/ spec §6.1。 |
| 2026-09-27 | **交互功能四件套 + 「没事了」（下线 §2.1–§2.4 与文末追加项）。** ①**问答/闲聊**：`IntentName.ASK`（规则层问句/问候触发词，排在具体话题后、弱搜索动词前）+ `LocalLlmBackend.generate` 自由生成（不复用 `complete()`），pipeline **先于 agent 拦截**（问答永无工具能力），答案过 `sanitize_for_tts`（≤80 字、剥英文/markdown）；「你好/谢谢/再见」免模型直接回；LLM 不可用明说「答不上来」。②**列目录**：`ToolName.LIST_DIR`（home 限制、目录优先、只念纯中文名、>4 项念「还有其他」、guest 拒绝）。③**二次确认回路**：`PipelineState.CONFIRMING`（免唤醒词喂 VAD/ASR）、`_PendingConfirmation`（原 `ToolCall` 重放 `confirmed=True` + tier/trace 绑定 + 15 s TTL）、取消词优先、非确认话=作废并按新话语重路由、换人/guest 不得确认。④**搜索先问后开**：`tools.search_web_confirm`（语音路径专用，agent 的 MCP 路径不受影响）。⑤**「没事了」**：`IntentName.DISMISS` 作为规则层兜底位，回「好的。」并回等待唤醒。新增配置 `llm.ask.*`、`tools.confirm_timeout_s`、`tools.search_web_confirm`（均热生效）。新增 `tests/unit/test_intent_rules_qa.py`、`tests/unit/test_list_dir.py`、`tests/integration/test_pipeline_qa.py`、`tests/integration/test_pipeline_confirmation.py`（全套 561 passed, 1 skipped）；顺带把 `test_a_verified_goal_does_not_rewrite_an_honest_failure` 改为密闭（原测试真跑 `taskkill`，开发机开着记事本时会把它关掉）。第 3 节新增两条缺口（问答依赖 llama-server、确认回路只覆盖语音路径）。同步 README / deployment（§5.1、§8.5、排障表）/ spec（§5、§6.1、§6.2、§7.1、§9.1、§14、§15）。 |
| 2026-09-27 | **杂音根因实锤 + 音高回调**。用户关闭 Windows「空间音效」（Spatial Sound）后杂音消失——设备层 DSP 实锤，与黑匣子「写入数据干净」的取证互相印证；deployment §10 排查行已更新为确认结论。音高按用户要求回调：`tts.pitch` 0.8 → **1.0**（原声；0.8 当初针对「童声感」，用户现觉「过于中性」，1.0/0.9/0.85/0.8 锚点已写入 config 注释与排查表）。机制与速度补偿不变。 |
| 2026-09-27 | **语速定档 0.9**。用户确认音色满意后反馈「发音过慢」（0.75 ≈ 250 ms/字 = 4.0 字/秒，处在自然区间慢端）。实测 0.9 → **208 ms/字 = 4.8 字/秒**（自然对话 4.5–5.5 的中位），`tts.speed` 0.75 → 0.9；访客 `guest_speed` 同步 0.9 → **1.0**（≈193 ms/字，保持比主人快 11 % 的区分度——两值曾同为 0.9 会失去区分）。锚点写入 config 注释：1.0 ≈ 193（播报腔）、0.8 ≈ 241、0.75 ≈ 250。需重启。同步 README / deployment（语速表 + 两行排查）/ spec §8 / §3 访客行。 |
| 2026-09-27 | **黑匣子实锤（数据层排除）**。用户在杂音现场用 `WINVOICE_DIAG_PLAYBACK` 抓到失败回合的写入数据（runtime/diag.pcm，5.3 s 一条回复，峰值正常）。全量分析：**逐 20 ms 帧对相关 0 次卡死重播（0/250）**、120 探测窗 **0 重复片段**、句内静音仅 1 个 200 ms 段（= 设计的句间停顿，位置 1.34 s）、无异常毛刺。结论：**应用写进设备的数据在杂音现场是干净的，杂音产生于设备/驱动/环境层**（应用之下）。设备侧证据待取：杂音在场时跑 `python scripts/diagnose_playback.py` 录扬声器实放，若出现卡死重播/重复片段即为实锤。 |
| 2026-09-27 | **叠加杂音排查（应用内 A/B + 黑匣子）**。用户关键对照：`python -m winvoice`（完整应用）有「覆盖一层规律卡顿的同一个音」，同期裸引擎+播放器脚本清晰 → 排除蓝牙/设备状态等环境因素，嫌疑收窄到应用内差异。逐项复现均干净：①真实 AudioPipeline（真 KWS tick/回合任务/get_time 链路）+ 真实播放器 + loopback 实录——0 卡死帧、0 重复片段；②加入**真实麦克风流**（16 k MME 输入与 WASAPI 输出共存）——发现**开麦后输出路由从 wasapi shared@48000 翻转为 wasapi auto-convert@44100**（复现稳定），但设备输出依然干净；③6 线程烧核 + 合成播放并发——无卡死重播；④双回合守卫核查无重叠播报。无法在可测条件下复现 → 交付两件仪器：`scripts/diagnose_playback.py`（写入 tee + loopback 实录 + 卡死重播/重复片段判定）与 `WINVOICE_DIAG_PLAYBACK` 环境变量（应用黑匣子：`SpeechPlayer.attach_diag` 把每个写入设备的样本存档，用户下次复现时即可用实盘数据一锤定音「我们的数据 vs 设备层」）。排查注意：soundcard loopback 按混音格式捕获且不重采样（本机 44.1 k），与 48 k 写入信号对比前必须伸缩校正；长块阻塞式 loopback 录制在负载下会丢帧，用逐秒分块。 |
| 2026-09-26 | **听感三轮：语流碎片化的真凶 + 音调定案**。用户反馈「一个字里面也有高频断点」。逐项排查 12 条清单后实锤两层根因：①**逐块重采样边缘伪影**——播放器对每个 100 ms 块独立 `resample_poly`，接缝处振幅台阶达峰值的 20–50 %（一个字 2–3 下，正是「字内断点」；此前用 10 ms RMS 窗验证边界是测量方法错误，把 0.5 ms 瞬态平均掉了）；②模型句内静音洞（见上轮）。修复：播放器改为**按段整段重采样**（段内各块本就微秒级连续入队，整段转换零额外首音延迟；接缝只剩段边界、被设计停顿掩蔽），伪影实测降到 int16 量化底噪（0.02 % 峰值）。音调定案：逐帧自相关 F0 实测 baker 原生**中位 276 Hz**（成人女声带 180–230、童声 280–350），「童声夹嗓子」= 模型嗓音本身 → `tts.pitch` 0.9 → **0.8**（≈221 Hz）；空隙压缩收紧为 60/30（实测 140–170 ms 洞全部压到 30、自然微停顿保留）。语速保持 246 ms/字、真机 underruns=0。新增按段重采样的连续性验证与默认值测试；同步 README / deployment / spec §8。**遗留**：baker 音色本身（读腔、flat pitch contour）不可调，想要「自然成人口语」只能换模型（kokoro-multi-lang / 多说话人 VITS，需下载 + 接入）。 |
| 2026-09-26 | **听感二轮：音调与卡顿**。用户反馈换 Matcha 后「音调偏高、一句话很多断点」。实测定位：①卡顿不是播放链路（真机 `underruns=0`），是模型**句内自掏 1–7 个 40–240 ms 静音洞**（词表无标点）→ 新增 `_common.collapse_internal_gaps`（`tts.max_internal_gap_ms`=110 以上压回 `internal_gap_keep_ms`=80，热生效）；②`speed` 实测**不变调**（对数频谱比对 1.000，推翻第一轮文档里「1.15≈92%」的旧测量），音调高是 baker 女声 + vocos 亮色 → 新增 `tts.pitch`（默认 0.9）：chunk 标称采样率 = 模型率 × pitch、`generate()` 用 `speed/pitch` 补偿语速，两旋钮解耦，**需重启**（已列入 REQUIRES_RESTART）。实测：语速保持 251 ms/字、压缩后句中静音 ≤110 ms。新增 `tests/unit/test_tts_internal_gaps.py`（9 项）与 pitch 数学 3 项。同步 README / deployment（调参表 + 排查两行）/ spec §8。 |
| 2026-09-26 | **TTS 架构改造（语义断句 + 停顿 + 22.05 kHz 输出）**。新增 `winvoice/text/`（规范化/语义断句/短句分块/停顿表，纯函数）、`winvoice/audio/playback.py`（常驻输出流 + 抖动词缓冲 + `abort()` 级打断 + 设备原生率重采样）、`_common.trim_edge_silence`（RMS 窗口剪掉每段 80–250 ms 噪声底）、`scripts/show_segmentation.py`、`scripts/calibrate_tts_pauses.py`；`TtsEngine` 改为多后端（matcha/vits）+ 模型回退 + 逐段合成 + 工作线程合成；`AudioPipeline` 回合改为 `asyncio.Task`（新增 `AGENT_THINKING`）；`TtsChunk` 增 `pause_after_ms`/`text`，`TtsRequest` 增 `max_chars`；默认模型换 `matcha-icefall-zh-baker` + `vocos-22khz-univ`（22 050 Hz），8 kHz 的 aishell3 转为回退模型。**下线 §2.0**（打断已实现）、**§1.2 部分下线**（已补 traceback 与回合异常日志）、**§2.1 的「先整句合成」一条改写为已完成（只剩 LLM 侧流式）**。新增第 0 节三条约束（停顿不是模型给的、逐段合成、播报长度走 `speech_context.speech_budget`、播放只有一个出口）与第 3 节两条缺口（访客只靠语速、LLM 侧不流式）。同步 README / deployment / spec §8 / DEPENDENCIES。 |
| 2026-09-20 | **DSH 集成**（`docs/dsh_integration_design.md`）。新增：`winvoice/dsh/`（SDK 客户端、程序化校验、两级升级、配置、桥接 bundle 生成）、`winvoice/mcp_server.py`（把工具注册表用 MCP 暴露给 DSH）、`winvoice/tools/verifier.py` + `state_capture.py`（用机器状态核对工具结果）、`winvoice/tools/utterance.py`（跨进程传说话人分级）、`winvoice/_vendor.py`、`scripts/install_dsh_bridge.py`；`contracts/speech.py` 新增 `sanitize_for_tts`；`tools/_coreaudio.py` 抽出 Core Audio 互操作（工具与验证器共用一份）。<br>**下线 1.1**（说话人分级已传到工具层，同步 README/spec §4.2/§6.5/§7.0）。<br>**新增 2.0**（agent 思考时无法打断——await 回合把唤醒词排队）、第 3 节三条（敏感应用名单、验证器覆盖 5/10、DSH 调用事件未解析）、第 0 节两条（不要用 `tmp_path`、跨进程分级靠 utterance.json）。<br>**与设计稿的偏差**（都记在 `docs/dsh_integration_design.md` §1a）：工具桥接从「生成 TypeScript 插件 + HTTP 桥」改为 **MCP**；验证器**不再改写** `ToolResult.success`（否则会把「好像没有在运行」这句实话改成「已经关闭了」）；只给**有可观测后置条件**的工具配验证器。 |
| 2026-09-19 | 建档。条目来源：`spec.md` §15「未实现清单」、`README.md` Known limitations，以及本次调试中实测确认的缺口（路由误判、浏览器弹窗、tier 未传、日志缺陷）。 |
| 2026-09-19 | 下线 原 §1.1「成功结果永远不会被念出来」、原 §2.3「查时间」、原 §2.4「查天气」。实现：`_spoken_success`（成功也念中文 `message`，可朗读 + ≤80 字规则统一放在 `winvoice/contracts/speech.py`）+ `ToolName.GET_TIME`/`GET_WEATHER` + `winvoice/tools/weather.py`（wttr.in，async + 整体 deadline，中文兜底，内置 WWO 码中文表）。同批同步 README / deployment / spec。原 1.2–1.4 与 2.5–2.7 已重编号为 1.1–1.3 与 2.3–2.5；新缺口写入第 0 节（成功也必须给 `message`、handler 可为 async）、1.1（新工具已声明 guest 但暂不生效）、2.4（`search_web` 只解决了一半）与第 3 节（天气数据源单一）。顺带修掉一个仓库陷阱：`.gitignore` 里未锚定的 `tools/` 连 `winvoice/tools/` 一起忽略，新增模块会静默进不了提交（已改为 `/tools/`）。 |
| 2026-09-19 | 实测报障修复（用户现场日志）：①「用浏览器搜索天气」被天气工具抢走，并把「览器搜索」当地名发给 wttr.in（500）→ 规则层改为三段优先级：**带路径参数的意图** > **显式搜索词**（`用浏览器/搜索/搜一下/百度/google/search`，拉丁词不得紧跟 `.`/`/`/`\`）> 表内顺序（`查一下/查询` 仍是弱触发词）；查询取「最后一个动词之后」的文本（`google 搜索天气` → 天气）；句子里的地名查不到时改用 `weather.city` 重查一次，网络类失败不换城市。②「打开谷歌浏览器」失败却报成功：`chrome.exe`/`msedge.exe`/`Code.exe` 都不在 `PATH`，改为 `resolve_app_command`（`App Paths` 注册表 →（`code` 用别名 `Code.exe`）→ `PATH`）按真实路径启动、去掉 shell、找不到就如实说；`close_app` 同样不再谎报（128/「找不到」→「好像没有在运行」，其余非 0（如 Access denied）→「我没能关掉…」，URI 目标→「我关不掉…」）；`search_web` 也不再把 `webbrowser.open()` 的 `False` 当成成功。③ query 改为 `quote()` 编码。新增 `tests/unit/test_app_launch.py`、`tests/unit/test_web_search.py`，并更新两个把旧缺陷当契约的旧测试。第 0 节、第 3 节、2.4、spec §5/§6.1、README、deployment 排障表已同步。 |


> 删除条目时请**只删条目**，并把同一次提交里同步过的文档（README / deployment / spec）
> 写进提交信息，方便回溯「哪次提交让它从这份文件里消失」。
