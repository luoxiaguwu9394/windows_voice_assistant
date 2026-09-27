# ADR 0001 — 安装向导架构：内嵌 Python 运行时 + 原始源码树，不冻结应用

- 状态：已采纳（2026-09-27）
- 背景：`spec.md` §12 曾把 "Packaging (Inno Setup)" 标记为 deferred；用户决定启动分发：
  「一个 exe，里面是 wizard，点击后解决所有 deployment 问题」，并补充了「要考虑更新」
  与「唤醒词可由用户设置」两个需求。

## 决策

`WinVoice-Setup-<版本>.exe`（PyInstaller onefile，内嵌 tkinter 向导与运行时载荷）。
载荷 = 内嵌 Python 3.12（依赖构建期装好并钉版）+ llama.cpp b7376 + `winvoice` 源码树
+ DSH `.pylibs`（含自带 Node 的单文件运行时），xz 压缩。模型（~3GB）不进载荷，由
向导在用户机上经 `scripts/download_models.py` 在线下载（断点续传 / 代理 / HF 镜像）。

**应用本体不做 PyInstaller/Nuitka 冻结。** 用户机安装目录复刻仓库布局：

```
<install>\winvoice\  scripts\  config\config.yaml  models\  tools\  .pylibs\
<install>\python\            # 内嵌解释器（python312._pth 里加了 ".."，安装根在 sys.path）
<install>\WinVoice-Setup-<版本>.exe   # setup exe 副本，双击重进向导
```

快捷方式目标 = `python\python.exe`，参数 `-m winvoice`，工作目录 = 安装根。

## 理由

1. **路径解析链原样成立，`winvoice` 包零改动。** 全应用按 CWD 解析
   （`config/config.yaml`、`models/`、`tools/*/llama-server.exe`、`logs/`），
   另有两条 `__file__` 上溯链（`dsh/settings.py` 的 `PROJECT_ROOT`、`_vendor.py` 的
   `.pylibs`）。安装目录 = 仓库布局 + CWD = 安装根（与 `run.ps1` 同一保证）时，
   这三条链无需任何代码改动。
2. **冻结会踩的具体坑**：`sys.executable -m winvoice.mcp_server` 被
   `install_dsh_bridge.py` 钉进 DSH bundle（冻结进程不支持 `-m`）；sherpa-onnx/
   scipy 的数据文件收集；onefile 下 `__file__` 指向临时目录。逐一修补的成本高于
   整包运行时（体积相同——依赖 + 模型本来就要带）。
3. **模型在线下载**（用户选定）：0.5B/1.5B/3B 按内存现选，避免 ~4GB 单文件分发；
   GitHub/HF 均支持断点续传，向导提供代理与 hf-mirror。
4. **DSH `.pylibs` 随载荷捆绑**：单文件运行时自带 Node，用户免装；由内嵌解释器
   自己安装（cp312 ABI 一致），杜绝把开发机 cp314 编译产物带进用户机。
   `install_dsh_bridge.py` 增加「捆绑运行时直调」回退，`dsh` 不再依赖 PATH/npm。

## 更新

安装标记 `<install>/.winvoice-install.json` 记版本。向导启动查 GitHub Releases
（资产 `WinVoice-Setup-<版本>.exe`），发现新版即在欢迎页提示、下载、引导重跑。
升级 = 重解压载荷：**载荷只含运行时自有目录**（python/winvoice/scripts/config 模板/
tools/.pylibs），不含 `models/`、`config/config.yaml`、`logs/`、`runtime/`、
`snapshots/`，因此重解压天然保留用户数据；模型下载幂等（已有文件跳过）。

## 后果

- 正面：应用代码零改动即可分发；升级不需要迁移逻辑；用户侧零 pip/编译。
- 负面：onefile 每次启动解压 ~300MB（数秒）；源码以明文随安装（无混淆，本来也非机密）；
  未签名 exe 会触发 SmartScreen/杀软提示。
- 弃替方案：PyInstaller 冻结应用本体（坑多、DSH 链路要改）、全离线单包（~4GB，
  构建与分发重）、极简在线安装器（用户侧 pip 故障面大）。

## 实现

- `installer/build_runtime.py` / `build_installer.py` / `README.md`
- `installer/wizard/`（core 纯逻辑 + tkinter UI）
- 新脚本 `scripts/audio_probe.py`；`download_models.py` 增 `--only` /
  `--progress-fmt machine` / `WINVOICE_HF_ENDPOINT`；`install_dsh_bridge.py` 增
  捆绑运行时回退；`config/config.template.yaml` 模板
- `requirements.txt` / `pyproject.toml` 补 `sentencepiece`、`pypinyin`（KWS 实需）
