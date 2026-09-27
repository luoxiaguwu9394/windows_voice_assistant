# 完善本地语音助手 —— 本地大模型闭环

**核心效果：部署从「先手动开终端 1 跑 llama-server，再跑 python -m winvoice」变成只跑 `python -m winvoice` 一个命令** —— 助手启动时自动探测并按需拉起/复用 llama-server，关闭时只清理自己拉起的。

## 1. llama-server 自动托管（核心功能）

现状：`deployment.md` 要求用户手动开「终端 1」跑 `llama-server`，忘了起服务则意图分类(Tier 2)、问答(ASK)、DSH 本地 agent 全部静默降级。winvoice 内无任何托管代码（已核实）。

**新增 `winvoice/llm/server.py` — `LlamaServerManager`：**
- `ensure_running()`：先健康检查 `{base_url}/health`（b7376 构建支持）——
  - 服务已在跑 → **直接复用，绝不重复拉起**；关闭助手时也**不动**用户手动起的服务；
  - 没在跑 → spawn `llama-server.exe -m <model_file> --port <port> -c <ctx>`（端口从 `llm.local.base_url` 解析；stdout/stderr 追加到 `logs/llama-server.log`，不弹窗口，Windows 用 `CREATE_NO_WINDOW`），轮询健康直到 `start_timeout_s`（默认 60s）。
- 成功：日志 `llama_server_started`（pid/port）；失败：`llama_server_failed`（附日志尾部），**不致命**——助手照常启动，规则层/寒暄可用，问答明说「答不上来」（沿用现有降级话术）。
- `shutdown()`：只 terminate 自己 spawn 的子进程。
- 接线：`VoiceAssistant.initialize()` 在建 intent router 之前调用；`shutdown()` 里释放。二进制自动发现：配置值 → `tools/*/llama-server.exe` glob → PATH。

**新配置键（加入 REQUIRES_RESTART，写入 config.yaml 带注释）：**
- `llm.local.auto_start: true`（设 false 回到今天的手动模式）
- `llm.local.server_binary: "tools/llama-b7376-bin-win-cpu-x64/llama-server.exe"`
- `llm.local.model_file: "models/llm/qwen2.5-3b-instruct-q4_k_m.gguf"`
- `llm.local.server_context: 4096`、`llm.local.start_timeout_s: 60`

**测试**（`tests/unit/test_llama_server_manager.py`，全用假 Popen/假健康检查，不碰真服务）：已健康→不 spawn；不健康→spawn+等待；spawn 失败→优雅返回 False；shutdown 只杀自己的子进程；二进制自动发现顺序。

## 2. 模型完整性：本地封存 + 启动校验（用户已选方案）

- `scripts/download_models.py` 加 `--seal`：遍历 MANIFEST 各模型目录，对每个文件算 SHA256+size 写入 `models/integrity.json`（含生成时间；支持 `--model <名>` 只封存单个）。**说明**：这是封存本机当前状态（能抓后续损坏/截断），不是官方源头校验——归档哈希需重新下载，作为剩余项记录。
- 新增 `winvoice/integrity.py`：`verify_sealed_models()`——逐条 size 快查（快路径，不拖慢启动）→ 不一致才算全量 SHA256 → 失败记 `model_integrity_failed`（路径+期望/实际大小）。**不做自动删除/改名**（`.corrupt/` 会从运行中的助手脚下抽走文件），只如实报告；`python -m winvoice --check` 逐模型打 `[OK]/[CHANGED]/[UNSEALED]`，有损坏时退出码 1。
- 接线：`VoiceAssistant.initialize()` 里做快路径校验；`run_check()` 做全量报告。
- **测试**（用仓库内 scratch 假模型树，遵守 §0「不用 tmp_path」）：封存→校验通过；篡改文件→校验失败并指出路径；未封存→跳过不报错。

## 3. 文档同步 + UNIMPLEMENTED 收尾

- `deployment.md`：§6「终端 1」改为**可选**（auto_start 默认开）；§5.1 新配置键；排障表加两行（spawn 失败/端口被占、完整性校验失败怎么办）。
- `README.md`：启动说明简化；「问答依赖 llama-server」限制改写。
- `spec.md`：§7.1（server 生命周期）、§9.2（封存校验）、§15 状态。
- `UNIMPLEMENTED.md`：§3「问答依赖 llama-server」改写（自动托管解决"没起就废"；剩余=DSH no-tools profile）、§3「模型完整性」改写（剩余=官方归档哈希）；新增维护记录行。

## 明确不做（留在 UNIMPLEMENTED）

1.2/1.3 日志类、敏感应用名单、pytest 标记、Prometheus、磁盘配额、docs/adr、DSH profile 换 sdk-minimal（有启动失败风险需真机实验）、LLM 流式、AEC、多说话人模型、PySide6。

## 验收

1. 不开「终端 1」直接 `python -m winvoice`：日志出现 `llama_server_started`，问答「什么是量子力学」有回答。
2. 手动已起服务再启动助手：不重复 spawn，关闭助手后手动服务仍在。
3. `llm.local.auto_start: false` 或二进制/模型缺失：启动不报错、行为同今天。
4. `--seal` 后 `python -m winvoice --check` 全 `[OK]`；篡改任一模型文件后 --check 退出码 1 并指名文件。
5. `pytest tests -q` 全绿（当前基线 605 passed, 1 skipped）。