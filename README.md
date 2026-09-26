# 小红书调研助手 (xhs-research-agent)

一个本地运行的问答智能体：你用自然语言提问，它会用**你自己登录的小红书账号**实时搜索、阅读笔记和评论区，然后给出带原帖链接的回答。

> A local chat agent that researches Xiaohongshu (RED) for you: ask a question, it searches notes and comments with your own logged-in account and answers with citations. Works with Claude Code, or any OpenAI-compatible API (DeepSeek, Qwen, Kimi, GLM, OpenAI…) with your own key.

## 功能

- **问答式**：直接提问，比如「手冲新手第一台磨豆机买什么？预算 500」。搜什么词、要不要换个角度再搜，都由智能体自己决定。
- **看得见过程**：每次搜索、读到第几篇笔记，界面上都实时显示；回答逐字输出。
- **有据可查**：回答里的结论都附带原帖链接和点赞/收藏数，评论区里的反对意见和疑似营销的笔记也会标出来。
- **支持图片**：可以粘贴、拖入或上传截图、菜单、商品照片等，助手会先看懂图片再决定要不要去搜。
- **可以追问**：同一个对话里追问时，能用已经读过的笔记回答的就不会重新抓取。
- **笔记浏览**：点「查看」可以浏览抓到的笔记卡片，按赞/藏/评排序，点开看全文、图片和评论。

## 用哪个大模型回答？

在界面左下角的「⚙ 设置」里二选一：

| 方式 | 适合谁 | 费用 |
|---|---|---|
| **Claude Code** | 已经订阅 Claude、装了 Claude Code 的人 | 计入 Claude 订阅用量 |
| **大模型 API** | 其他所有人 | 自己注册平台、充值、填 API Key，按用量付费。费用取决于所选模型和问题的复杂度，以平台定价为准 |

大模型 API 内置了几个服务商的预设：DeepSeek、通义千问（阿里云百炼）、Kimi、智谱 GLM、OpenAI。也可以填任何兼容 OpenAI 格式的接口地址。
设置里可以「获取模型列表」和「测试连接」。

- 分析图片需要支持看图的模型（比如通义的 `qwen-vl` 系列、智谱的 `glm-4v` 系列）。截至编写时，DeepSeek 的对话模型还不能看图。各家的模型名称和能力变化很快，请以平台文档为准。
- API Key 只保存在本机的 `data/settings.json`（权限 600，不会进 git）。
- 每个对话固定用创建时选的模型；换了设置后，新对话才会用新的模型。

## 工作原理

```
浏览器 GUI ──► FastAPI (xhs_reader/server.py)
                 ├─► Claude Code：claude -p（只允许运行 ./xhs research / digest）
                 └─► 大模型 API：xhs_reader/llm.py（函数调用 search_xiaohongshu / read_previous_notes）
                        └─► ./xhs → Playwright + 本机 Chrome（持久化登录状态）──► 小红书网页版
```

- 抓取：用 Playwright 驱动本机的 Google Chrome，通过监听网页自身的接口响应拿到搜索结果和评论，笔记详情从页面状态里读取。
- 推理：两种后端用同一份提示词（`xhs_reader/agent_prompt.md`）。API 模式下，每轮最多搜 3 次、24 篇的限制由代码强制执行，不依赖模型是否听话。

## 下载安装（推荐）

到 [Releases](https://github.com/StellarStar255/xhs-research-agent/releases) 下载最新版：

- **macOS（Apple 芯片 M1 及以后）**：下载 `.dmg`，打开后把「小红书调研助手」拖进「应用程序」，然后双击打开。正式版本经过 Apple 签名和公证，可以直接打开；如果提示「无法验证」，点「完成」，然后到**系统设置 → 隐私与安全性**里点「仍要打开」。
- **Windows 10/11（64 位）**：下载 `-setup.exe` 安装，从开始菜单或桌面打开。安装时如果出现「Windows 已保护你的电脑」，点「更多信息 → 仍要运行」（Windows 版没有代码签名）。

需要先安装 [Google Chrome](https://www.google.com/chrome/)（Windows 上没有 Chrome 时会使用系统自带的 Edge）。

在「设置 → 打开方式」里可以二选一，随时切换，会记住你的选择：

- **独立窗口**（默认）：像普通软件一样有自己的窗口，Dock（Windows 是任务栏）里有图标；调研进行中，Dock 图标上会显示数字。关闭窗口即退出，有调研正在进行时会先问你。
- **浏览器**：页面在你的浏览器里打开。macOS 上 Dock 图标会隐藏，改为屏幕右上角菜单栏里的一个小图标，点开可以看状态（空闲 / 正在调研）、打开页面、切换回独立窗口、打开设置、退出。Windows 上会保留一个最小化的任务栏窗口，关闭它即退出。

顶部的应用菜单里还可以新建对话、扫码登录、打开数据文件夹。如果电脑缺少内置浏览器组件（少数 Windows 电脑），会自动改为在浏览器里打开。
**自动更新**：应用启动后会检查 GitHub 上有没有新版本（之后每 6 小时一次），有的话左上角会提示「新版本可用」，点「立即更新」就会自动下载、校验并重启到新版本。也可以在「设置 → 版本」里手动检查。
安装前会校验：macOS 版必须带有本项目开发者的签名，Windows 版必须和 Release 里的 `SHA256SUMS.txt` 一致，否则拒绝安装。如果应用不在可写的位置（比如直接从 DMG 里运行），会打开新版本的安装包让你手动替换。

你的登录状态、对话记录和设置保存在 `~/.xhs-research-agent`（Windows：`%USERPROFILE%\.xhs-research-agent`），卸载或升级应用都不会删除它们。

## 从源码运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./start.sh       # 打开 http://localhost:8766（端口被占用时自动换一个）
```

源码运行时，数据保存在项目里的 `data/` 目录。

第一次打开时，页面会引导你：
1. **登录小红书**：点「扫码登录小红书」会弹出一个 Chrome 窗口，用小红书 App 扫码即可。登录状态保存在 `data/profile`，一般能保持很久；过期了，助手会在对话里提示你重新扫码。
2. **选大模型**：装了并登录了 Claude Code 的，会自动使用它；没有的话，会提示你在「设置」里填 API Key。

## 命令行

```bash
./xhs status                          # 查看登录状态
./xhs research "关键词" -n 12 -c 15    # 搜索并输出摘要（智能体用的工具）
./xhs search "关键词" -n 20            # 只抓取，保存到 data/research/<id>/
./xhs digest <id>                     # 重新输出某次抓取的摘要
./xhs list
./xhs selftest                        # 检查能否启动本机浏览器（不访问小红书）
```

环境变量：

- `XHS_AGENT_MODEL=sonnet`：让回答更快
- `XHS_PORT=8766`：修改端口
- `XHS_DATA_DIR=~/xhs-data-2`：换一个数据目录（相当于另一个独立的账号和对话记录）
- `XHS_CLAUDE_PATH=/path/to/claude`：Claude Code 装在不常见的位置时，手动指定路径
- `XHS_UI=browser`：完全不启用原生外壳（没有窗口、Dock 图标和菜单栏图标），只在浏览器里打开
- `XHS_NO_BROWSER=1`：只启动后台服务，不打开窗口或浏览器

## 打包和发布

推送 `v*` 标签（版本号要和 `xhs_reader/__init__.py` 里的一致）后，GitHub Actions 会自动打包 macOS 和 Windows 版本、运行冒烟测试，并发布到 Releases。也可以在 Actions 页面手动运行，只打包不发布。
macOS 签名和公证需要在仓库的 Secrets 里配置 Apple 开发者证书，具体见 [.github/workflows/release.yml](.github/workflows/release.yml) 开头的说明。
也可以在本地签名：先用 `xcrun notarytool store-credentials <名字>` 保存公证凭证，再设置 `MACOS_SIGN_IDENTITY` 和 `NOTARY_PROFILE=<名字>` 运行下面的签名脚本。本地打包：

```bash
.venv/bin/pip install -r packaging/requirements-build.txt
.venv/bin/python packaging/gen_licenses.py
.venv/bin/pyinstaller packaging/xhs-research-agent.spec --noconfirm
packaging/macos_sign_notarize.sh "dist/XHS Research Agent.app" dist/app.dmg   # macOS
```

安装包里附带了所有第三方组件的许可证（`THIRD_PARTY_LICENSES.txt`）。

## 访问频率限制

这个工具用的是你自己的账号，所以默认访问得很慢、很少，既保护你的账号，也尽量减少对平台的访问压力：

- **数量上限**：每次搜索默认 8 篇、每轮对话最多约 24 篇；每小时最多 40 篇、每天最多 150 篇
- **放慢节奏**：每两篇笔记之间都会停顿一段时间
- **自动停止**：如果小红书提示访问过于频繁，会立即停止，并在 3 小时内不再访问
- 同一时间只有一个浏览器实例在运行

`./xhs limits` 可以查看剩余额度和暂停状态。

## ⚠️ 免责声明

- 本项目**仅供个人学习和研究使用**，与小红书（行吟信息科技）没有任何关联。
- 应用第一次打开时会显示「使用须知」，同意后才能使用；之后可以在「设置」里重新查看。
- 自动化访问可能违反《小红书用户服务协议》，账号可能因此被限制或封禁。**风险由使用者自行承担。**
- **禁止**用于：商业用途；大规模或高频抓取；出售、公开或再分发抓取到的内容和数据；绕过验证码等安全措施；任何侵犯他人著作权、隐私或个人信息权益的行为。
- 笔记和评论的著作权归原作者所有。抓取到的数据只保存在本地的 `data/` 目录，请勿外传，并定期清理。
- 使用本项目即表示你理解并同意：因使用本项目产生的一切后果由使用者自行承担，与作者无关。
- 如果相关权利人认为本项目侵犯了其合法权益，请提 issue 联系，我们会及时处理。

## 注意

小红书网页版改版很频繁。如果某天搜索结果为 0，多半是页面结构或接口变了，需要更新 `xhs_reader/scraper.py` 里的选择器和接口路径，欢迎提 PR。

## License

[MIT](LICENSE)
