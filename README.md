# Caddie Hosted Alpha Gateway

这个服务只代理 AI 请求，不承载 Caddie 的用户数据库、文档或求职资料。

必需环境变量：

- `DEEPSEEK_API_KEY`：仅保存在云平台环境变量中的官方模型 Key。
- `CADDIE_GATEWAY_TESTERS_JSON`：每位测试者独立凭证与额度。

示例（不要提交真实 token）：

```json
{
  "tester-01": {"token": "生成的随机长字符串", "daily_calls": 50, "daily_tokens": 300000},
  "tester-02": {"token": "另一条随机长字符串", "daily_calls": 50, "daily_tokens": 300000}
}
```

桌面端添加 OpenAI 兼容供应商：

- Base URL：`https://你的-gateway-域名/v1`
- API Key：分配给该测试者的 token
- Model：`deepseek-chat`

Gateway 只保存 tester ID、日期、调用次数和 token 数，不保存请求正文或回复正文。
