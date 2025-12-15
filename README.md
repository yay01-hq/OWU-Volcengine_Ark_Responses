# Volcengine Ark Responses
# 火山方舟 Responses API Pipeline（OpenWebUI）

这是一个 OpenWebUI 的管线函数（pipe），用于通过火山方舟（Volcengine Ark）的 Responses API（`/api/v3/responses`）调用模型，并解决 **工具调用卡住**、**用量统计缺失** 等常见问题。

## 功能概览

- **修复工具调用卡住**：在工具开始/结束时主动发送状态事件，避免界面一直停留在“调用工具”。
- **工具调用超时**：为每次工具执行增加超时（默认 90 秒），并将超时/异常转为可见输出。
- **多轮工具链**：支持模型发起多次 function_call，按轮次执行并把结果回填给模型继续推理。
- **用量统计（usage）补全**：当流式响应没有返回用量时，通过 `GET /responses/{id}` 轮询补齐用量。
- **兼容 OpenWebUI 用量面板**：在流式输出末尾额外发送 ChatCompletions 风格的 `usage` chunk，便于 OpenWebUI 展示 Token 消耗。
- **会话续写（previous_response_id）**：开启 `store` 后复用 `previous_response_id`，减少重复上下文与费用；当 system 或 tools 发生变化会自动重置会话，避免 400。
- 支持火山方舟的上下文缓存（需要在火山方舟网页手动开启）。
- **仅测试了OenAPI的工具，MCP/流式HTTP目前不可用。**

## 运行环境

- OpenWebUI：`0.4.0` 或更高
- Python 依赖：`httpx>=0.24.0`（其余依赖通常随 OpenWebUI 已包含）

## 安装

1. 在 OpenWebUI 进入 **Functions**（函数）页面。
2. 选择 **Import**（导入），导入本仓库提供的函数导出文件（`.json`），或新建 pipe 并粘贴 `content` 中的代码。
3. 启用该函数，并在模型配置中选择此 pipe 作为后端管线。
4. 需要在模型-高级参数-函数调用中开启“原生”。

> 不同版本的 OpenWebUI 菜单位置略有差异，但核心思路一致：**导入/创建 pipe → 启用 → 在模型里选用该 pipe**。

## 配置（Valves）

该函数通过 Valves 提供可视化配置项（可在 OpenWebUI 函数设置里修改）：

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | 火山方舟 API 基础地址 |
| `API_KEY` | 空 | API Key（建议留空，改用环境变量） |
| `MODEL_ID` | `kimi-k2` | 模型 ID（按你的火山方舟控制台实际填写） |
| `TEMPERATURE` | `1.0` | 采样温度 |
| `TOP_P` | `0.7` | nucleus sampling |
| `MAX_OUTPUT_TOKENS` | `None` | 生成上限（可由请求 `max_tokens` 覆盖） |
| `THINKING_TYPE` | `None` | `thinking.type`（enabled/disabled/auto） |
| `REASONING_EFFORT` | `None` | `reasoning.effort`（minimal/low/medium/high） |
| `ENABLE_WEB_SEARCH` | `False` | 追加 `web_search` 工具（若模型支持） |
| `WEB_SEARCH_LIMIT` | `10` | `web_search` 返回条数上限 |
| `STORE` | `True` | 允许使用 `previous_response_id` 续写 |
| `ENABLE_CACHING` | `True` | 发送 `caching: {type: enabled}` |
| `ENABLE_SESSION_CACHE` | `True` | 在同一对话中复用 `previous_response_id` |
| `MAX_TOOL_ROUNDS` | `15` | 工具最大轮次，防止死循环 |
| `TOOL_TIMEOUT_SECONDS` | `90` | 单次工具调用超时（秒） |
| `FORCE_CHAT_COMPLETIONS_SSE` | `False` | 即使不请求用量也强制走 SSE 输出 |
| `FORWARD_STREAM_OPTIONS` | `False` | 是否把 `stream_options` 转发给 `/responses`（通常不支持） |
| `USAGE_RETRIEVE_RETRIES` | `3` | 用量查询重试次数 |
| `USAGE_RETRIEVE_DELAY_MS` | `200` | 用量查询重试间隔（毫秒） |
| `DEBUG` | `False` | 打印调试信息 |
| `TIMEOUT` | `600` | HTTP 请求超时（秒） |

### API Key 的安全配置

推荐把 Key 放到环境变量，而不是写进函数文件：

- 环境变量名：`VOLCENGINE_API_KEY`
- Valves 里 `API_KEY` 留空即可自动读取环境变量

示例（Docker）：

```bash
docker run -e VOLCENGINE_API_KEY="<YOUR_VOLCENGINE_API_KEY>" ...
```

## 工作机制（简述）

1. **消息输入**：保留 system 角色内容，把 OpenWebUI 的 messages 转为 Responses API 的 `input` 结构。
2. **流式解析**：消费火山方舟的 SSE 事件，把 `output_text.delta` 直接转发给前端；将 `reasoning_summary_text.delta` 包裹为 `<think>...</think>` 输出。
3. **工具调用**：当收到 `function_call` 项目后，按 call_id 执行对应工具（支持超时），再把结果作为 `function_call_output` 回填继续下一轮。
4. **用量统计**：优先从流式事件中读取 `usage`；缺失时用 `GET /responses/{id}` 重试补齐；最后以 ChatCompletions SSE 的 `usage chunk` 形式发给 OpenWebUI。

## 常见问题

- **报错 `unknown field "stream_options"`**  
  `/responses` 往往不支持 `stream_options`。保持 `FORWARD_STREAM_OPTIONS=False`；本函数也会在遇到该 400 时自动移除后重试一次。

- **报错与 `previous_response_id` + `tools` 相关**  
  某些后端在复用 `previous_response_id` 时不允许再次发送 `tools`。本函数只在首轮附带 `tools`，并在检测到 400 时自动移除后重试。

- **工具执行很慢或疑似卡死**  
  调小 `TOOL_TIMEOUT_SECONDS`，或减少 `MAX_TOOL_ROUNDS`。超时会以文本输出回填给模型，避免整条对话阻塞。

## 许可协议

MIT
