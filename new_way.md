Task: Integrate DeepSeek Harness (DSH) into Windows Voice Assistant

0. Project Goal

You are working on my existing project:

"windows_voice_assistant"

Repository:
https://github.com/luoxiaguwu9394/windows_voice_assistant

The goal is to integrate DeepSeek Harness (DSH) directly into the existing Windows Voice Assistant so that:

«User speaks → ASR converts speech to text → DSH Agent receives the request → DSH uses an LLM + tools to plan and execute actions on the Windows computer → result is returned → TTS speaks the result.»

I want DSH to become the Agent / computer-control layer of this project.

Do NOT rewrite the whole project from scratch.

First inspect the existing repository carefully and understand the current architecture, process model, audio pipeline, LLM routing, tool system, permissions, configuration, and IPC mechanism.

---

1. First: Analyze Before Coding

Before modifying anything:

1. Read the entire README.
2. Inspect the project tree.
3. Identify:
   - Audio process
   - ASR
   - KWS
   - VAD
   - Speaker verification
   - TTS
   - Local LLM
   - Cloud LLM
   - Intent router
   - Tool registry
   - Execution process
   - Permission system
   - Confirmation system
   - Configuration system
   - IPC/message passing
4. Find the current entry point.
5. Find where recognized user text currently enters the LLM/intent pipeline.
6. Find where tool execution currently happens.
7. Determine the cleanest integration point for DSH.

Do not start blindly editing files.

First produce a short architecture assessment and an implementation plan.

Then implement it.

---

2. Target Architecture

The target architecture should approximately become:

Microphone
↓
KWS / VAD
↓
ASR
↓
User Text
↓
DSH Agent
↓
LLM
↓
DSH Agent Loop
↓
Tools
↓
Windows
↓
Tool Result
↓
DSH
↓
Final Response
↓
TTS
↓
Speaker

The important conceptual change is:

DSH becomes responsible for Agent reasoning, planning, tool calling, and computer control.

The existing audio system should remain responsible for:

- microphone input
- wake word
- VAD
- ASR
- TTS

Do not unnecessarily replace the existing audio pipeline.

---

3. DSH Integration

Use the official DeepSeek Harness implementation and documentation.

Before implementation, inspect the current official DSH repository/API because DSH is still evolving and APIs may change.

Official repository:

https://github.com/deepseek-ai/deepseek-harness

Do not assume APIs from old examples.

Use the version/API that is actually compatible with the current project environment.

If DSH provides an official Python API/CLI/runtime suitable for embedding, prefer the official integration mechanism.

If DSH works better as a subprocess/service, that is acceptable.

The integration must be reliable on Windows.

---

4. Model Backend

The project currently has both:

- Local LLM
- Cloud LLM

I want DSH to be able to use the existing model architecture rather than creating an unrelated third LLM system.

Design the integration so that DSH can receive a configured model backend.

Ideally:

Local model:

Voice Assistant
→ ASR
→ DSH
→ Local LLM
→ DSH tools
→ Windows

Cloud model:

Voice Assistant
→ ASR
→ DSH
→ Cloud LLM
→ DSH tools
→ Windows

If DSH requires a specific model/provider interface, create the smallest adapter necessary.

Do not duplicate model configuration unnecessarily.

Keep API keys and model configuration in the existing configuration system.

Never hard-code API keys.

---

5. Computer Control

The most important requirement:

DSH must be able to actually control the Windows computer.

Start with safe, useful tools.

At minimum investigate and implement support for:

- PowerShell / shell commands
- launching applications
- reading files
- writing files
- creating directories
- moving/copying files
- querying system information
- opening URLs
- basic Windows automation where appropriate

Prefer DSH's existing official tools when they already provide the required functionality.

Do NOT duplicate an existing DSH capability with unnecessary custom code.

For capabilities that DSH does not provide directly, create small project-specific tools.

---

6. Tool Safety

Do not give the model unrestricted arbitrary execution without safeguards.

The Agent must have a clear tool boundary.

Every tool should have:

- name
- description
- input schema
- execution function
- permission level
- destructive/non-destructive classification
- timeout
- structured result

For example:

ToolResult:

{
"success": true,
"message": "...",
"data": {}
}

or:

{
"success": false,
"message": "...",
"error": "..."
}

The Agent must never claim an operation succeeded when the tool actually failed.

---

7. Confirmation System

Integrate DSH with the existing confirmation/permission design.

Operations such as:

- deleting files
- overwriting important files
- running potentially destructive commands
- executing arbitrary scripts
- modifying system settings
- installing software
- changing security settings

should require confirmation where appropriate.

The flow should be:

User request
↓
DSH plans action
↓
Tool identifies destructive operation
↓
Assistant asks for confirmation
↓
User says yes/no
↓
Continue or cancel

Do not bypass the existing safety architecture just because DSH can execute shell commands.

If DSH has its own approval/permission mechanism, integrate with it rather than implementing a competing system unnecessarily.

---

8. Voice Interaction

The user should be able to say things like:

"Open VS Code."

"Open my project."

"Create a folder called test on my desktop."

"Find the largest files in my Downloads folder."

"Open Chrome and search for today's weather."

"Create a Python file called hello.py and write a simple hello world program."

The pipeline should be:

Speech
→ ASR
→ DSH
→ Agent reasoning
→ Tool calls
→ Result
→ Natural language response
→ TTS

The user should NOT need to manually type commands.

---

9. Multi-Step Tasks

Do not restrict DSH to one tool call.

The system should support multi-step tasks.

Example:

User:

"Open VS Code and open my Windows Voice Assistant project."

Possible execution:

1. Locate VS Code.
2. Launch VS Code.
3. Locate project directory.
4. Open project.
5. Verify success.
6. Report result.

Another example:

"Find all Python files in my Downloads folder and put them into a new folder called PythonFiles."

Possible plan:

1. Inspect Downloads.
2. Find .py files.
3. Create destination directory.
4. Move files.
5. Verify.
6. Report result.

The Agent should be able to observe tool results and continue execution based on those results.

---

10. Keep Audio Responsive

Do not block the audio process while DSH is thinking or executing tools.

The architecture should remain process-isolated where appropriate.

Prefer:

Audio Process
↓
IPC
↓
Agent / DSH Process
↓
Tool Execution

rather than:

Audio Process
↓
blocking DSH call
↓
audio freezes

The microphone/audio pipeline must remain responsive.

---

11. Process Architecture

Preserve the existing multi-process architecture unless there is a strong technical reason to change it.

A reasonable target is:

Main
├── Audio Process
│   ├── KWS
│   ├── VAD
│   ├── ASR
│   └── TTS
│
├── DSH / Agent Process
│   ├── Context
│   ├── Model
│   ├── Planning
│   ├── Tool Calling
│   └── Agent Loop
│
└── Execution / Tool Process
└── Windows operations

However, inspect the existing implementation before deciding the exact process boundaries.

Do not introduce unnecessary IPC complexity.

---

12. Context and Conversation

DSH should maintain enough conversation context for natural voice interaction.

For example:

User:
"Open VS Code."

Assistant:
"Done."

User:
"Now open my project."

The second request should be understandable in context.

However, avoid keeping unlimited conversation history in memory.

Implement reasonable context/session management based on DSH's capabilities.

---

13. Configuration

Add configuration options for DSH without breaking existing configuration.

For example:

dsh:
enabled: true
model: ...
provider: ...
workspace: ...
confirmation_required: true

The exact configuration format should follow the project's existing conventions.

Do not invent an entirely new configuration architecture.

The user should be able to switch between local and cloud models through configuration.

---

14. Logging

Add structured logging around:

- ASR result
- DSH request
- selected model
- tool call
- tool arguments
- tool result
- confirmation request
- execution failure
- final response

Never log:

- API keys
- passwords
- authentication tokens
- sensitive credentials

Logs should make debugging Agent behavior easy.

Example:

[ASR]
text="open vscode"

[DSH]
model="local"
intent="computer_task"

[TOOL]
name="open_app"
args={"app":"Visual Studio Code"}

[TOOL]
success=true

[TTS]
response="VS Code is open."

---

15. Error Handling

Handle at least:

- DSH unavailable
- model unavailable
- model timeout
- malformed model output
- tool timeout
- tool failure
- invalid tool arguments
- permission denied
- user cancellation
- ASR failure
- TTS failure

Never crash the entire voice assistant because one Agent task failed.

Return a useful user-facing response.

For example:

"I couldn't open VS Code because it wasn't found on your system."

not:

"Done."

---

16. Testing

Add tests for the DSH integration.

At minimum:

1. DSH initialization
2. Local model request
3. Cloud model request
4. Simple tool call
5. Multi-step tool call
6. Invalid tool arguments
7. Tool failure
8. Confirmation required
9. User cancellation
10. DSH unavailable
11. Model timeout
12. Audio process remains responsive

Where possible, mock the LLM and Windows tools.

Do not make tests dependent on a real API key.

---

17. Do Not Overengineer

Important:

Do NOT rewrite the entire project.

Do NOT replace working components just because DSH provides similar functionality.

Do NOT add unnecessary frameworks.

Do NOT create multiple overlapping tool systems.

Do NOT duplicate DSH's functionality unless there is a clear project-specific reason.

The goal is:

Existing Voice Assistant
+
DSH Agent
+
Windows control

not:

Completely new project.

---

18. Documentation

After implementation, update README with:

- new architecture diagram
- DSH installation
- configuration
- model configuration
- local model usage
- cloud model usage
- Windows permissions
- available tools
- confirmation behavior
- example voice commands
- troubleshooting

Include a clear diagram such as:

Microphone
↓
KWS/VAD
↓
ASR
↓
DSH Agent
↓
LLM
↓
Tools
↓
Windows
↓
DSH
↓
TTS

---

19. Development Strategy

Implement incrementally.

Phase 1:

ASR
→ DSH
→ simple response
→ TTS

Phase 2:

DSH
→ one safe Windows tool
→ verify execution

Phase 3:

DSH
→ multiple Windows tools
→ structured results

Phase 4:

confirmation system

Phase 5:

multi-step Agent tasks

Phase 6:

local/cloud model selection

Phase 7:

robustness, logging and tests

After every phase:

- run tests
- run the application
- verify the feature
- fix errors before moving on

Do not make hundreds of unrelated changes in one step.

---

20. Coding Style

Follow the existing project's coding style.

Prefer:

- small modules
- typed interfaces
- clear schemas
- explicit error handling
- dependency injection where useful
- testable components

Avoid:

- giant files
- global state
- hidden side effects
- hard-coded paths
- hard-coded API keys
- shell commands embedded throughout the codebase

---

21. Important Requirement

The final user experience should feel like:

I say:

"Hey assistant, open Chrome and search GitHub for my project."

Then:

ASR
→ DSH
→ model reasoning
→ browser/Windows tools
→ execution
→ result
→ spoken response

The user should not need to know what tool was used.

---

22. Final Deliverables

When finished, provide:

1. Summary of architecture changes.
2. List of files changed.
3. Explanation of how DSH is integrated.
4. Explanation of how local and cloud models work.
5. List of available computer-control tools.
6. Explanation of confirmation/security boundaries.
7. Tests added.
8. Commands to install/run the system.
9. Known limitations.
10. Suggested next development steps.

Most importantly:

Actually implement the changes in the repository. Do not only give me a theoretical answer.

If you encounter a problem with the current DSH API, inspect the current official DSH source/documentation and adapt to the actual API instead of inventing an API.

Before finishing, run the available tests and perform at least one end-to-end smoke test of:

Voice/Text input
→ DSH
→ Model
→ Windows tool
→ Tool result
→ final response.
对，这样更合理。你原来的三级降级架构不要删，只是把 DSH 放到“模型 Agent 执行层”里。

核心逻辑应该是：

用户语音
   ↓
ASR
   ↓
规则匹配
   │
   ├── 能确定 → 直接执行 Tool
   │
   └── 无法确定
          ↓
      本地模型 + DSH
          │
          ├── 正常完成 → 返回结果
          │
          └── 不可靠 / 失败 / 不确定
                    ↓
               云端模型 + DSH
                    ↓
                 执行

这里有个关键点：不能单纯因为“本地模型回答得像胡说八道”就凭感觉切云端。最好给本地 Agent 一个明确的“是否可信”的判定机制。

比如：

Rule Engine
    ↓
Local DSH Agent
    ↓
┌─────────────────────────────┐
│ Validation                  │
│                             │
│ ✓ 输出符合 Tool Schema      │
│ ✓ Tool 存在                 │
│ ✓ 参数合法                  │
│ ✓ 权限允许                  │
│ ✓ Tool 执行成功             │
│ ✓ 结果与目标一致            │
└──────────────┬──────────────┘
               │
        ┌──────┴──────┐
        ↓             ↓
      PASS          FAIL
        ↓             ↓
      返回       Cloud DSH Agent

尤其是不要让本地模型自己说“我很确定”然后决定自己是否可信。应该由你的程序检查它的输出和实际 Tool Result。

你可以把之前 Prompt 的核心部分替换成这一版

Core Agent Architecture

Do NOT remove the existing rule-matching system.

The project must preserve a three-level execution strategy:

User Speech
    ↓
ASR
    ↓
Rule Matching
    │
    ├── Match confidently
    │       ↓
    │    Direct Tool Execution
    │
    └── No reliable match
            ↓
       Local DSH Agent
            │
            ├── Validated + successful
            │        ↓
            │      Result
            │
            └── Failed / unreliable
                     ↓
                Cloud DSH Agent
                     ↓
                  Tools
                     ↓
                  Result

Level 1 — Rule Matching

Rules are the fastest and most deterministic path.

Use rules for simple, high-frequency commands such as:

- volume up/down
- mute/unmute
- pause/play
- open a known application
- close a known application
- lock the computer
- basic system operations

When a rule matches with sufficient confidence, DO NOT invoke an LLM.

Execute the corresponding tool directly.

The rule system should remain fast and deterministic.

---

Level 2 — Local Model + DSH

When no reliable rule matches:

Send the user's request to the local LLM through DSH.

The local model is responsible for:

- understanding natural language
- planning actions
- selecting tools
- executing multi-step tasks
- interpreting tool results

Example:

User:

"Find all Python files in my Downloads folder and put them into a new folder."

Pipeline:

ASR
 ↓
Rule miss
 ↓
Local DSH
 ↓
Local LLM
 ↓
Tool calls
 ↓
Filesystem
 ↓
Tool results
 ↓
DSH
 ↓
Final response

The local model should be preferred because it is faster, cheaper, and can operate locally.

---

Level 3 — Cloud Model + DSH

If the local Agent cannot reliably complete the task, escalate to the cloud model.

Do NOT escalate merely because the local model produces an unusual response.

Escalation should be based on observable conditions.

Examples:

1. Local model produces invalid structured output.
2. Tool name does not exist.
3. Tool arguments fail schema validation.
4. Local model repeatedly chooses invalid tools.
5. Local Agent enters an execution loop.
6. Tool execution fails and the local model cannot recover.
7. The model explicitly indicates that it cannot determine the correct action.
8. The task requires capabilities unavailable to the local Agent.
9. A configurable confidence/validation threshold is not satisfied.
10. The local Agent times out.

Then:

Local DSH
    ↓
Validation / Execution Failure
    ↓
Cloud DSH
    ↓
Retry / Re-plan
    ↓
Tool execution

---

Local Model Reliability

Do NOT allow the local model to decide its own reliability solely through natural language.

Implement programmatic validation.

For every local Agent task, collect:

AgentResult
├── success
├── final_response
├── tool_calls
├── tool_results
├── validation_errors
├── execution_errors
├── elapsed_time
└── escalation_reason

The system should determine whether escalation is required.

Example:

if local_result.success:
    return local_result

if local_result.invalid_tool_call:
    escalate("invalid_tool_call")

elif local_result.timeout:
    escalate("timeout")

elif local_result.tool_execution_failed:
    escalate("tool_failure")

elif local_result.validation_failed:
    escalate("validation_failure")

else:
    escalate("unknown_failure")

---

Important: Verify Actual Results

Never treat:

LLM says:
"Done!"

as proof that the operation succeeded.

Instead:

LLM
 ↓
Tool
 ↓
Actual Windows result
 ↓
Validation
 ↓
Success / Failure

For example:

User:

"Open VS Code."

Local model:

open_app("Visual Studio Code")

Tool:

success = false
error = "Application not found"

The system must NOT return:

"VS Code is open."

Instead, the local Agent should first attempt recovery.

If recovery fails:

Local Agent failed
        ↓
Cloud Agent
        ↓
Cloud Agent may search for the correct executable
        ↓
Retry

---

DSH Placement

DSH should be used by both model levels:

                    ┌───────────────┐
                    │ Rule Matching │
                    └───────┬───────┘
                            │
                       no match
                            ↓
                    ┌───────────────┐
                    │ Local DSH     │
                    │ + Local LLM   │
                    └───────┬───────┘
                            │
                       validation
                            │
                  ┌─────────┴─────────┐
                  ↓                   ↓
               success              failure
                  ↓                   ↓
               Result          ┌───────────────┐
                               │ Cloud DSH     │
                               │ + Cloud LLM   │
                               └───────┬───────┘
                                       ↓
                                     Tools

Do not create a completely separate tool system for the cloud model.

Both Local DSH and Cloud DSH should use the same project Tool Registry whenever possible.

This ensures that:

Local Agent
       │
       ├── open_app
       ├── read_file
       ├── write_file
       ├── search_files
       └── run_command

Cloud Agent
       │
       ├── open_app
       ├── read_file
       ├── write_file
       ├── search_files
       └── run_command

use the same execution and permission boundaries.

---

Escalation Must Preserve Context

When escalating from Local DSH to Cloud DSH, provide enough context for the cloud Agent to continue rather than starting blindly from zero.

For example:

{
  "user_request": "...",
  "local_attempt": "...",
  "tool_calls": [...],
  "tool_results": [...],
  "errors": [...],
  "escalation_reason": "local_tool_failure"
}

The cloud Agent should understand what the local Agent already attempted.

Avoid unnecessarily repeating successful operations.

---

Security Boundary

Both local and cloud Agents must use the same permission and confirmation system.

The cloud model must NOT automatically receive broader permissions simply because it is more capable.

For example:

Rule
   ↓
Local DSH
   ↓
Cloud DSH

all ultimately pass through:

Permission
   ↓
Confirmation
   ↓
Tool Registry
   ↓
Execution

No model should be able to bypass these boundaries.

---

Performance Goal

The intended behavior is:

Simple command:

Voice
 ↓
ASR
 ↓
Rule
 ↓
Tool

Fast.

Complex but locally solvable:

Voice
 ↓
ASR
 ↓
Rule miss
 ↓
Local DSH
 ↓
Tool

No cloud API required.

Difficult or failed local task:

Voice
 ↓
ASR
 ↓
Rule miss
 ↓
Local DSH
 ↓
Validation / execution failure
 ↓
Cloud DSH
 ↓
Tool

This should minimize cloud usage while maintaining reliability.

---

Final Requirement

The existing Rule → Local LLM → Cloud LLM architecture is a core design requirement.

Do NOT replace it with:

Everything → DSH → Cloud

Instead implement:

Rule
  ↓
Local DSH + Local LLM
  ↓
Cloud DSH + Cloud LLM

with programmatic validation and automatic escalation.

The final system should behave like a hybrid computer Agent:

Rules handle the obvious things.
The local model handles most intelligent tasks.
The cloud model acts as the fallback when the local Agent cannot reliably solve the task.

Keep this architecture modular so that the local model, cloud model, DSH runtime, and tools can be changed independently.我会特别推荐你采用这个版本，因为它保留了你原来架构里**“简单任务不要浪费 LLM，复杂任务先本地，搞不定再上云”**的核心思想，同时让 DSH 真正负责电脑控制。

可以把 Verifier（验证器） 设计成一个独立于 LLM 的“裁判层”。核心原则就是：

> LLM 只负责“我想做什么、怎么做”；Verifier 不听 LLM 怎么说，只检查 Windows 的实际状态。



你的项目特别适合做成下面这个结构：

用户语音
   ↓
ASR
   ↓
规则匹配
   ├── 命中 → 直接执行 → Verifier
   │
   └── 未命中
         ↓
      Local DSH
         ↓
      Tool Executor
         ↓
      Verifier
         │
     ┌───┴────────┐
     ↓            ↓
   成功          失败
     │            │
     ↓            ↓
    返回      Local Retry
                  ↓
             仍失败？
                  ↓
             Cloud DSH

1. Verifier 不应该是一个“大模型”

我建议第一版不要让 LLM 来验证 LLM。

例如用户说：

> “打开 Chrome。”



Local DSH：

> open_application("chrome")



Executor 执行后，不要直接相信：

{
  "success": true
}

而应该：

Tool Executor
    ↓
实际执行 CreateProcess / Windows API
    ↓
Verifier
    ↓
检查 chrome.exe 是否真的存在
    ↓
检查进程是否启动
    ↓
SUCCESS

也就是说：

execution_result = executor.execute(action)

verification_result = verifier.verify(
    action=action,
    execution_result=execution_result
)

最终成功应该是：

success = (
    execution_result.ok
    and verification_result.verified
)

而不是：

success = execution_result.llm_says_success


---

2. 最重要的是设计 VerificationSpec

不要让 Verifier 自己猜“这个任务怎么验证”。

每一种 Tool 都应该定义自己的验证规则。

例如：

class VerificationSpec:
    tool_name: str
    checks: list
    timeout: float

打开程序

OpenApplicationVerifier(
    process_name="chrome.exe"
)

验证：

1. chrome.exe 是否存在？
2. 进程是否正常运行？
3. 如果要求前台打开：
   当前 foreground window 是否属于 Chrome？


---

创建文件

用户：

> “在桌面创建 test.txt。”



Tool：

create_file(
    path="C:/Users/.../Desktop/test.txt"
)

Verifier：

检查：
    文件是否存在
    是否是普通文件
    文件路径是否正确


---

删除文件

用户：

> “删除 test.txt。”



Verifier：

检查：
    test.txt 是否已经不存在

注意这里就出现一个很重要的问题：

Tool 执行失败 ≠ 最终任务一定失败。

例如：

删除文件
   ↓
Windows API 返回异常
   ↓
Verifier 检查
   ↓
文件其实已经不存在
   ↓
任务可以视为完成

所以 Verifier 应该拥有最终判断权。


---

3. 我建议把结果设计成 4 种状态

不要只有：

True / False

而是：

class VerificationStatus(Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    NOT_VERIFIABLE = "not_verifiable"

例如：

VERIFIED

{
  "status": "verified",
  "reason": "chrome.exe is running"
}

FAILED

{
  "status": "failed",
  "reason": "Requested file does not exist"
}

UNCERTAIN

例如：

> “帮我整理一下桌面。”



程序确实移动了一些文件，但是“整理得好不好”无法通过简单规则判断。

{
  "status": "uncertain",
  "reason": "File movements completed, but organization quality cannot be objectively verified"
}

NOT_VERIFIABLE

例如：

> “写一篇好看的文章。”



程序可以验证：

文件创建成功
内容写入成功

但很难仅靠 Windows 状态验证：

“文章写得好不好”

所以：

{
  "status": "not_verifiable"
}


---

4. 更关键：Verifier 应该检查“状态变化”

这是你这个项目最值得做的一部分。

例如用户说：

> “把桌面的 test.txt 移到 Documents。”



执行之前：

Desktop/test.txt       EXISTS
Documents/test.txt    NOT EXISTS

保存一个：

before_state

执行：

move_file(...)

然后检查：

after_state

得到：

Desktop/test.txt       NOT EXISTS
Documents/test.txt     EXISTS

于是：

Before
  ↓
执行
  ↓
After
  ↓
比较
  ↓
Verified

这比单纯检查：

Tool 返回 success

可靠很多。


---

5. 可以把 Verifier 做成插件式架构

你的 Tool Registry 本身就可以扩展成：

Tool Registry
│
├── open_application
│   └── verifier
│
├── close_application
│   └── verifier
│
├── create_file
│   └── verifier
│
├── delete_file
│   └── verifier
│
├── move_file
│   └── verifier
│
├── copy_file
│   └── verifier
│
├── set_volume
│   └── verifier
│
└── ...

例如：

class ToolDefinition:
    name: str
    schema: dict
    executor: Executor
    verifier: Verifier

这样 DSH 根本不需要知道验证细节。

它只需要：

调用 Tool
    ↓
系统执行
    ↓
系统验证
    ↓
把验证结果反馈给 DSH


---

6. Verifier 的返回值也应该结构化

我建议类似这样：

{
  "status": "failed",
  "verified": false,
  "checks": [
    {
      "name": "process_exists",
      "passed": true
    },
    {
      "name": "window_foreground",
      "passed": false
    }
  ],
  "reason": "Application started but did not become foreground window",
  "retryable": true
}

这样 Local DSH 就能看到：

Tool → 执行了
Verifier → 没完全成功
原因 → xxx
retryable → true

然后它可以重新规划。


---

7. 这样就能解决你之前说的“本地模型自信地撒谎”

比如：

用户：
打开计算器，然后输入 12345

Local DSH：

1. open calculator
2. type 12345

第一步：

Executor → 成功
Verifier → Calculator.exe 存在
Verifier → VERIFIED

第二步：

Executor → SendInput
Verifier → 检查计算器窗口内容
Verifier → FAILED

不要问 Local DSH：

> “你觉得刚才成功了吗？”



而是直接告诉它：

{
  "tool": "type_text",
  "execution": {
    "ok": true
  },
  "verification": {
    "status": "failed",
    "reason": "Expected text was not detected"
  }
}

然后：

Local DSH
   ↓
看到 verification failed
   ↓
重新规划
   ↓
重试

如果：

Retry 1 → FAILED
Retry 2 → FAILED

再：

Cloud DSH


---

8. 甚至可以做“分层验证”

我很推荐你的项目采用：

Level 0
执行结果验证

Level 1
系统状态验证

Level 2
目标状态验证

Level 3
任务结果验证

比如：

> “把 Chrome 打开并访问 example.com。”



Level 0

CreateProcess 成功？

Level 1

chrome.exe 存在？

Level 2

Chrome 窗口存在？
example.com 页面加载？

Level 3

页面最终状态是否符合用户要求？

只有达到对应任务要求的验证级别：

VERIFIED


---

9. 最终我建议你的核心接口长这样

class Verifier(ABC):

    @abstractmethod
    def verify(
        self,
        action,
        execution_result,
        expected_state,
        context
    ) -> VerificationResult:
        pass

然后：

class VerificationResult:

    status
    verified
    checks
    reason
    retryable
    evidence

整个 Agent Loop：

while not task_finished:

    action = dsh.plan(context)

    execution = executor.execute(action)

    verification = verifier.verify(
        action,
        execution,
        expected_state,
        context
    )

    if verification.status == VERIFIED:
        context.add_success(verification)
        continue

    if verification.status == FAILED and verification.retryable:
        context.add_failure(verification)
        continue

    if verification.status in (
        FAILED,
        UNCERTAIN,
        NOT_VERIFIABLE
    ):
        escalate_to_cloud()


---

10. 最终架构

结合你现在的 规则 → Local DSH → Cloud DSH，我会建议最终变成：

┌──────────────┐
                    │     Voice    │
                    └──────┬───────┘
                           ↓
                         ASR
                           ↓
                  ┌─────────────────┐
                  │   Rule Engine   │
                  └────────┬────────┘
                           ↓
                    未命中可靠规则
                           ↓
                  ┌─────────────────┐
                  │   Local DSH     │
                  └────────┬────────┘
                           ↓
                    Tool Planning
                           ↓
                  ┌─────────────────┐
                  │ Tool Executor   │
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │    Verifier     │
                  │                 │
                  │ Before State    │
                  │       ↓         │
                  │ Execution       │
                  │       ↓         │
                  │ After State     │
                  │       ↓         │
                  │ Verification    │
                  └────────┬────────┘
                           ↓
                    ┌──────┴──────┐
                    │             │
                 VERIFIED       FAILED
                    │             │
                    ↓             ↓
                  Done       Local Retry
                                  │
                             still failed
                                  ↓
                           ┌─────────────┐
                           │ Cloud DSH   │
                           └──────┬──────┘
                                  ↓
                              Tool Executor
                                  ↓
                               Verifier

最关键的一点：Verifier 不负责“理解用户”，也不负责“判断 LLM 说得有没有道理”。它只负责一个问题：系统当前的真实状态，是否符合这个 Action 的预期结果？

这样你的本地模型即使出现“我已经完成了”的幻觉，系统也不会直接接受它。