# 小红书调研助手 (xhs_reader)

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
| **大模型 API** | 其他所有人 | 自己注册平台、充值、填 API Key，按用量付费（一个问题通常几毛钱以内，以平台定价为准） |

大模型 API 内置了几个服务商的预设：DeepSeek、通义千问（阿里云百炼）、Kimi、智谱 GLM、OpenAI。也可以填任何兼容 OpenAI 格式的接口地址。
设置里可以「获取模型列表」和「测试连接」。

- 分析图片需要支持看图的模型（比如通义的 `qwen-vl` 系列、智谱的 `glm-4v` 系列）。DeepSeek 目前的对话模型不能看图。
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

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./start.sh       # 打开 http://localhost:8766
```

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
```

环境变量：

- `XHS_AGENT_MODEL=sonnet`：让回答更快
- `XHS_PORT=8766`：修改端口
- `XHS_DATA_DIR=~/xhs-data-2`：换一个数据目录（相当于另一个独立的账号和对话记录）
- `XHS_CLAUDE_PATH=/path/to/claude`：Claude Code 装在不常见的位置时，手动指定路径

## 内置的防风控措施

小红书会对短时间内大量打开笔记详情页的账号限流（「访问频繁」，错误码 300013）。实测 40 分钟内打开约 100 篇就会触发。所以：

- **慢速浏览**：每篇笔记之间随机停顿 6–12 秒，每读 4–6 篇再休息 20–40 秒，打开笔记后会先滚动几下
- **数量上限**：每次搜索默认 8 篇、每轮对话最多约 24 篇；硬上限为每小时 40 篇、每天 150 篇
- **自动冷却**：一旦识别到风控页面，立即停止抓取并冷却 3 小时，期间拒绝所有抓取
- 同一时间只有一个浏览器实例（`data/browser.lock`）

可以用环境变量调整：`XHS_DELAY_MIN` / `XHS_DELAY_MAX` / `XHS_HOURLY_CAP` / `XHS_DAILY_CAP` / `XHS_COOLDOWN_HOURS`。**不建议调松。**
`./xhs limits` 可以查看剩余额度和冷却状态。

## ⚠️ 免责声明

- 本项目**仅供个人学习和研究使用**，与小红书（行吟信息科技）没有任何关联。
- 自动化访问可能违反[小红书用户协议](https://www.xiaohongshu.com)，账号可能因此被限制或封禁。**风险由使用者自行承担。**
- **禁止**用于：商业用途；大规模或高频抓取；出售、公开或再分发抓取到的内容和数据；绕过验证码等安全措施；任何侵犯他人著作权、隐私或个人信息权益的行为。
- 笔记和评论的著作权归原作者所有。抓取到的数据只保存在本地的 `data/` 目录，请勿外传，并定期清理。
- 使用本项目即表示你理解并同意：因使用本项目产生的一切后果由使用者自行承担，与作者无关。
- 如果相关权利人认为本项目侵犯了其合法权益，请提 issue 联系，我们会及时处理。

## 注意

小红书网页版改版很频繁。如果某天搜索结果为 0，多半是页面结构或接口变了，需要更新 `xhs_reader/scraper.py` 里的选择器和接口路径，欢迎提 PR。

## License

[MIT](LICENSE)
