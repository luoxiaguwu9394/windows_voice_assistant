# WinVoice 安装器（Setup Wizard）

本目录是把 WinVoice 做成「用户双击一个 exe 即可完成全部部署」的构建系统。

## 产物

| 文件 | 作用 |
|---|---|
| `installer/payload/runtime.tar.xz` | 运行时载荷：内嵌 Python 3.12 + 全部依赖 + llama.cpp + winvoice 源码 + DSH `.pylibs`（由 `build_runtime.py` 生成，不进 git） |
| `installer/_dist/WinVoice-Setup-<版本>.exe` | 单文件安装向导（PyInstaller，载荷内嵌；版本号来自 `pyproject.toml`） |

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

## 架构（为什么不是 PyInstaller 冻结整个应用）

应用本体**不冻结**：用户机安装目录 = 仓库布局（`winvoice/`、`scripts/`、`config/`、
`models/`、`tools/`、`python/`……），启动时工作目录即安装目录。整条路径解析链
（CWD 相对的 config/models/logs、`__file__` 上溯的 DSH `PROJECT_ROOT`、`.pylibs`）
因此原样成立，`winvoice` 包零改动。完整决策见 `docs/adr/0001-installer-wizard-architecture.md`。

## 向导流程

1. 欢迎（系统信息 / 检测已装实例 → 升级 / 更新横幅）
2. 安装位置（默认 `%LOCALAPPDATA%\WinVoice`，校验可写与空间）
3. 网络（代理 / HF 镜像 / 连通性测试）
4. 解压运行时载荷 + 写默认 `config.yaml`
5. 下载模型（按内存推荐 0.5B/1.5B/3B；断点续传；跑完 `--seal` 生成完整性封存）
6. 音频设备（`scripts/audio_probe.py` 用内嵌解释器跑真实播放路由 + 录音电平）
7. 个性化（城市 / **唤醒词编辑器** / DSH 开关 / 云端 key → `setx`）
8. DSH 桥接安装（`install_dsh_bridge.py --install`，走捆绑运行时，无需 npm）
9. 声纹注册（弹控制台跑 `winvoice.enroll`，可选）
10. 自检（`python -m winvoice --check`，退出码 0 才放行）
11. 快捷方式（桌面/开始菜单/自启）+ 写安装标记 + 复制 setup exe 到安装目录

**升级**：重新运行安装器 → 欢迎页识别安装标记 → 升级模式重解压载荷（载荷不含
`models/`、`config/config.yaml`、`logs/`、`runtime/`、`snapshots/`，用户数据天然保留）
→ 模型步骤幂等跳过已有文件。**更新发现**：向导启动时查 GitHub Releases 最新版，
发现新 `WinVoice-Setup-*.exe` 即在欢迎页提示下载并引导重跑。

## 手动测试清单（每次发版前）

- [ ] 干净目录全新安装：向导全程 → `--check` 通过 → 桌面快捷方式启动 → 说唤醒词走通一轮
- [ ] 唤醒词页添加/删除词条 → 生成的 `config.yaml` `kws.keywords` 正确 → 改词后重启助手生效
- [ ] 断网重试下载：杀掉下载进程后重跑向导，模型从断点续传
- [ ] 已装目录重跑安装器：欢迎页出现三个模式；升级模式后 `config.yaml` 与 `models/` 原样
- [ ] 取消勾选 DSH：`config.yaml` 中 `dsh.enabled: false`，助手正常启动
- [ ] 勾选开机自启：`shell:startup` 出现 lnk；取消勾选后被移除
- [ ] 中文/空格路径：出现黄色警告但可继续安装

## 已知限制

- onefile 安装器每次启动需解压 ~300MB 载荷（数秒无响应窗口，属正常）；
- 未做代码签名：SmartScreen 会拦第一次运行（「仍要运行」），杀软可能误报
  （若误报严重，改 `--onedir` + zip 分发，架构不变）；
- 自动更新 = 提示 + 下载新安装器重跑；无静默后台更新。
