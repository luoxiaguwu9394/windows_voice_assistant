# WinVoice 安装器（Setup Wizard）

本目录是把 WinVoice 做成「用户双击一个 exe 即可完成全部部署」的构建系统。

## 产物

| 文件 | 作用 |
|---|---|
| `installer/payload/runtime.tar.xz` | 运行时载荷：内嵌 Python 3.12 + 全部依赖 + llama.cpp + winvoice 源码 + DSH `.pylibs`（由 `build_runtime.py` 生成，不进 git） |
| `installer/_dist/WinVoice-Setup-<版本>.exe` | 单文件安装向导（PyInstaller，载荷内嵌；版本号来自 `pyproject.toml`） |
| `installer/_dist/WinVoice-Setup-<版本>.exe.sha256` | 上一行的校验和（sha256sum 格式；构建自动生成，**发布时两个都要上传**——向导下载后会校验，不匹配即删除报错） |

## 构建（开发机）

```powershell
# 1. 运行时载荷（需要网络；llama.cpp 优先复用仓库 tools/ 里的已验证版本）
python installer/build_runtime.py
python installer/build_runtime.py --reuse-pylibs   # 离线时直接复制仓库 .pylibs（仅当确认 ABI 兼容）

# 2. 安装器 exe（载荷缺失时会自动先跑第 1 步）
pip install pyinstaller          # 首次
python installer/build_installer.py
```

版本戳写入 `installer/wizard/core/_version.py`（自动生成，**不要提交**）。更新检查按
Release 资产名 `WinVoice-Setup-<版本>.exe` 对比版本，发版时保持这个命名。

构建时有两道守卫（对应 UNIMPLEMENTED.md §3 踩过的坑）：

1. **版本号两处一致**：`pyproject.toml` 与 `winvoice/__init__.py` 的 `__version__`
   不一致直接拒绝构建（两处都要 bump）；
2. **同版本号拒绝**：最新已发布 Release 已是该版本号时拒绝构建——同号重打包对已装
   机器永远不可见（更新比对是严格递增）。确认无误可加 `--allow-same-version` 强行构建；
   开发机离线时守卫只警告不拦截。

## 发版检查单（每次发布 GitHub Release）

- [ ] bump 版本号：`pyproject.toml` **和** `winvoice/__init__.py` 两处
- [ ] `python installer/build_installer.py`（守卫自动校验上面两项 + 同号检查）
- [ ] 上传**两个**资产到 GitHub Release：`WinVoice-Setup-<版本>.exe` + `.exe.sha256`
- [ ] **老向导还在字段上时按非预发布发布**：旧版向导查 `/releases/latest`，它排除
      pre-release（实测该端点在预发布仓库恒 404）。0.1.2-dev 及之前发的版本都受此约束
- [ ] 在一台旧版本机器上开向导：欢迎页出现更新横幅 → 下载 → 校验通过 → 引导重跑
- [ ] 在已装最新版的机器上开向导：不出现横幅（幂等）

> 向导侧排障：更新检查/下载失败会写
> `%LOCALAPPDATA%\WinVoice\logs\setup-wizard.jsonl`（卸载时随目录删除）。检查失败
> 永远不打扰用户（不弹窗、横幅不出现），但原因会留在日志里。

## 架构（为什么不是 PyInstaller 冻结整个应用）

应用本体**不冻结**：用户机安装目录 = 仓库布局（`winvoice/`、`scripts/`、`config/`、
`models/`、`tools/`、`python/`……），启动时工作目录即安装目录。整条路径解析链
（CWD 相对的 config/models/logs、`__file__` 上溯的 DSH `PROJECT_ROOT`、`.pylibs`）
因此原样成立，`winvoice` 包零改动。完整决策见 `docs/adr/0001-installer-wizard-architecture.md`。

## 向导流程

1. 欢迎（系统信息 / 检测已装实例 → 升级 / 更新横幅）
2. 安装位置（默认 `%LOCALAPPDATA%\WinVoice`，校验可写与空间；从安装目录启动复制的向导会管理该目录；更新下载器会带上原安装路径）
3. 网络（代理 / HF 镜像 / 连通性测试）
4. 解压运行时载荷 + 写默认 `config.yaml`
5. 下载模型（按内存推荐 0.5B/1.5B/3B；断点续传；跑完 `--seal` 生成完整性封存）
6. 音频设备（`scripts/audio_probe.py` 用内嵌解释器跑真实播放路由 + 录音电平）
7. 个性化（城市 / **唤醒词编辑器** / DSH 开关 / 云端 key → `setx`）
8. DSH 桥接安装（`install_dsh_bridge.py --install`，走捆绑运行时，无需 npm）
9. 声纹注册（弹控制台跑 `winvoice.enroll`，可选）
10. 自检（`python -m winvoice --check`，退出码 0 才放行）
11. 快捷方式（桌面/开始菜单/自启）+ 写安装标记 + 复制 setup exe 到安装目录。
    桌面与开始菜单各有**两个**：`WinVoice 语音助手`（助手本体）与
    `WinVoice 设置`（`-m winvoice.webui` 本地配置界面：应用白名单/唤醒词/语音参数/天气城市，永不自启）

> **升级/卸载前会先停掉占用安装目录的进程**（助手，以及它复用但不持有的 llama-server）。不停的话，重解压 `tools/` 会撞上被占用的 `llama-server.exe` —— 表现就是**卡在 92 %**（该偏移正好是载荷里 `tools/` 的起点；2026-09-28 实测）。解压期间会在进度条下显示「正在写入：<文件>」，慢盘/杀软扫描时不再像卡死。

**升级**：重新运行安装器 → 欢迎页识别安装标记 → 升级模式重解压载荷（载荷不含
`models/`、`config/config.yaml`、`logs/`、`runtime/`、`snapshots/`，用户数据天然保留）
→ 模型步骤幂等跳过已有文件。**更新发现**：向导启动时查 GitHub Releases 最新版，
发现新 `WinVoice-Setup-*.exe` 即在欢迎页提示下载并引导重跑。Release 检查会遍历分页；
下载失败会清理不完整安装器。卸载的后台清理器通过 PowerShell 字面路径删除目录，
进程归属按规范化后的真实目录边界判断。

## 手动测试清单（每次发版前）

- [ ] 干净目录全新安装：向导全程 → `--check` 通过 → 桌面快捷方式启动 → 说唤醒词走通一轮
- [ ] 唤醒词页添加/删除词条 → 生成的 `config.yaml` `kws.keywords` 正确 → 改词后重启助手生效
- [ ] 断网重试下载：杀掉下载进程后重跑向导，模型从断点续传
- [ ] 已装目录重跑安装器：欢迎页出现四个模式（升级 / 重新配置 / 直接启动 / 卸载）；升级模式后 `config.yaml` 与 `models/` 原样
- [ ] 取消勾选 DSH：`config.yaml` 中 `dsh.enabled: false`，助手正常启动
- [ ] 助手或 llama-server 正在运行时执行升级：在「安装运行时」页应看到「已停止 llama-server…」，解压不再停在 92 %
- [ ] 一键卸载：选「卸载」→ 确认 → 运行时 / 快捷方式 / 安装标记删除；勾选保留时 `models/` 与 `config/config.yaml` 留存，安装目录在窗口关闭后被删掉
- [ ] 勾选开机自启：`shell:startup` 出现 lnk；取消勾选后被移除
- [ ] 中文/空格路径：出现黄色警告但可继续安装
- [ ] 「WinVoice 设置」快捷方式：双击打开设置页 → 「扫描本机程序」/手动添加应用 → 保存 → `config.yaml` 更新且注释保留 → 重启助手后说「打开<该应用>」能开（白名单项）或被如实拒绝（未安装）

## 已知限制

- onefile 安装器每次启动需解压 ~300MB 载荷（数秒无响应窗口，属正常）；
- 未做代码签名：SmartScreen 会拦第一次运行（「仍要运行」），杀软可能误报
  （若误报严重，改 `--onedir` + zip 分发，架构不变）；
- 自动更新 = 提示 + 下载新安装器重跑；无静默后台更新。
