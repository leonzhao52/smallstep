# smallstep — 个人 AI Agent

跑在本地浏览器里的通用对话 Agent，后端接任意 OpenAI 兼容服务。支持**多 Provider + 有优先级的模型自动回退**、流式输出、工具调用、会话管理、附件上传。

## 功能

**对话核心**
- 多 Provider（OpenAI / DeepSeek / 本地 Ollama 等任何 OpenAI 兼容服务）
- 有优先级的 model 列表，当前 model 不可用时**自动回退**到下一个
- 流式输出（SSE），前端逐字渲染
- Markdown 渲染 + 代码高亮（带 XSS 消毒）
- 会话记录存为 JSON 文件，支持多会话、历史回看
- 会话标题自动生成（首轮对话后用 LLM 起标题）

**会话管理**
- 重命名、归档、软删除（回收站）、恢复、永久删除
- 侧栏分区展示：活跃 / 已归档 / 回收站

**内置工具**（Agent 可自主调用）
- `web_search` — 联网搜索（Tavily，未配 key 时降级到 DuckDuckGo）
- `web_fetch` — 抓取网页正文（转 Markdown）
- `read_file` / `write_file` / `list_dir` — 本地文件读写（沙箱限制在项目目录内）
- `execute_command` — 执行 shell 命令，**带风险分级 + 用户确认机制**

**安全**
- 文件工具的敏感路径黑名单：`.env`、`config.yaml`、`data/` 等不可读
- `execute_command` 白名单制：只有只读命令自动执行，其它（写/删/装包/管道…）一律弹窗请用户确认
- Markdown 输出经 DOMPurify 消毒，防 XSS
- 附件上传：扩展名白名单、大小上限、文件名消毒（防路径穿越）

**附件上传**
- 支持文本类文件（`.py`/`.js`/`.md`/`.json`/`.yaml` 等）和图片（`.png`/`.jpg`/`.webp`/`.gif`）
- 文本内容内联进当前消息；图片转 base64 走 vision（需模型支持）
- 自动探测编码（UTF-8 / GBK / Big5 等），中文 Windows 文件不乱码

**个人微信渠道**（可选）
- 用逆向的 iLink 纯 HTTP 协议（参考 nanobot），**无需本地微信客户端**，扫码登录后长轮询收发消息
- 第一版只支持**文本收发**；每个微信联系人一个独立会话
- `allowed_users` 白名单控制谁能对话；微信端遇 `execute_command` 高风险命令**自动拒绝**（无网页确认按钮）
- 启用：在 **WebUI 设置面板 → 接入渠道 → 微信** 勾选启用并保存（**热生效，无需重启**），点「扫码登录」二维码直接显示在页面里扫码即可（不再打印到终端）
- ⚠️ 第三方逆向协议（非官方），无稳定性/封号保证，可能随时失效，自行判断风险

## 快速开始

```bash
pip install -r requirements.txt

# 配置
cp config.example.yaml config.yaml
cp .env.example .env
# 编辑 .env 填入密钥；编辑 config.yaml 调整 provider 和 model_priority

# 运行
python -m uvicorn app.main:app --reload
```

浏览器打开 http://127.0.0.1:8000

## 配置说明

`config.yaml` 不含密钥，用 `${ENV_VAR}` 或 `${ENV_VAR:-默认值}` 从 `.env` 注入。

`model_priority` 是有优先级的候选列表，从上到下依次尝试：
```yaml
model_priority:
  - provider: openai      # 高优先级
    model: gpt-4o-mini
  - provider: deepseek    # 第一个失败时回退到这里
    model: deepseek-chat
```

想验证回退：把第一个候选项的 model 故意填成不存在的名字，发消息时界面会出现「已切换模型」提示并成功用第二个出结果。

`.env` 里至少配一个你要用的 provider 密钥，例如：
```
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=...
TAVILY_API_KEY=...   # 可选，配了联网搜索用 Tavily
```

## 添加自定义工具

在任意模块里用装饰器注册即可，无需改 agent 循环：
```python
from app.tools import register

@register(
    name="read_file",
    description="读取本地文件内容",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "文件路径"}},
        "required": ["path"],
    },
)
async def read_file(path: str) -> str:
    ...
```
在 `app/tools.py` 的 `_load_builtin_tools()` 里 import 你的模块以触发注册。

## 目录结构

```
app/
├── agent.py        # 核心：跨 model 回退循环 + 工具调用循环
├── config.py       # 配置加载 + ${VAR} 占位符渲染 + pydantic 校验
├── providers.py    # AsyncOpenAI 客户端池（按 provider 缓存）
├── llm.py          # 单次流式请求
├── routes.py       # API 路由（SSE 聊天、会话管理、上传、确认）
├── storage.py      # 会话 JSON 存取
├── tools.py        # 工具注册表
├── tool_files.py   # 文件读写工具（沙箱 + 敏感路径保护）
├── tool_web.py     # 联网搜索 + 网页抓取
├── tool_exec.py    # execute_command 注册
├── cmd_exec.py     # 命令风险分级 + 子进程执行
├── approvals.py    # 高风险命令的确认请求管理
├── attachments.py  # 附件上传存储 + 编码探测
├── log.py          # 日志（控制台 + 滚动文件）
└── main.py         # FastAPI 入口
static/index.html   # 单文件前端（原生 HTML/JS/CSS）
data/               # 运行时数据（会话、日志、上传，已 gitignore）
```

## 已知限制

- 单用户、无鉴权，设计为本地个人使用（不要直接暴露到公网）
- 图片识图需要所配模型支持 vision（如 gpt-4o）
- 日文 Shift-JIS 文本可能被编码探测误判为 Big5（中文 GBK/Big5 场景正常）

## License

[MIT](LICENSE)
