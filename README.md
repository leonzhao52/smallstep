# 个人 Agent 骨架

本地 Web 界面的通用对话 agent，支持多 LLM provider + 有优先级的 model 回退、流式输出、JSON 会话存储。**当前是骨架**：对话循环、流式管道、工具调用扩展点已就绪，具体工具待后续添加。

## 功能
- 多 provider（OpenAI / DeepSeek / 本地 Ollama 等 OpenAI 兼容服务）
- 有优先级的 model 列表，当前 model 不可用自动回退到下一个
- 流式输出（SSE），前端逐字渲染
- 会话记录存为 JSON 文件，支持多会话、历史回看
- 工具调用框架（注册表为空，接口已留好）

## 快速开始
```bash
pip install -r requirements.txt

# 配置
cp config.example.yaml config.yaml
cp .env.example .env
# 编辑 .env 填入密钥；编辑 config.yaml 调整 provider 和 model_priority

# 运行
uvicorn app.main:app --reload
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

## 添加工具（下一轮）
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

## 目录结构
见计划文件或源码注释。核心：
- `app/agent.py` — 跨 model 回退 + 工具循环（回退机制的落地点）
- `app/config.py` — 配置加载与校验
- `app/tools.py` — 工具注册表
- `static/index.html` — 聊天界面

## 已知限制（本轮范围外）
- Markdown / 代码高亮未渲染（纯文本展示）
- 工具未实现
- 无鉴权、单用户
- 会话标题取首条消息前 30 字
