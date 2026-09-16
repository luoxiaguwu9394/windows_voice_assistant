# windows_voice_assistant
WinVoice 是基于 sherpa-onnx 的 Windows 语音助手。本地优先，支持语音唤醒、声纹识别与电脑操控。三级意图路由（纯规则 → 本地小模型 → 云端 LLM），兼容本地 Ollama 与任意 OpenAI 兼容远程 API。工具白名单限制 LLM 权限，破坏性操作需二次确认并自动快照，可回溯。声纹分三级权限：完整、访客、拒绝；访客禁用云端与敏感操作，并切换声线。
