# Caddie AI Gateway

这个服务只代理 AI 请求，不承载 Caddie 的用户数据库、文档或求职资料。

必需环境变量：

- `DEEPSEEK_API_KEY`：仅保存在云平台环境变量中的官方模型 Key。
- `CADDIE_GATEWAY_ADMIN_TOKEN`：招募后台调用发码与账号管理接口的管理凭证。
- `CADDIE_WEBSITE_CALLBACK_URL`：用户激活后回写官网状态的完整 HTTPS 地址。
- `CADDIE_GATEWAY_CALLBACK_TOKEN`：Gateway 与官网共同持有的回调凭证。
- `CADDIE_GATEWAY_TESTERS_JSON`：旧内测用户兼容凭证；新用户不再写入这里。
- `SUPABASE_URL`：产品分析项目地址，例如 `https://xxx.supabase.co`。
- `SUPABASE_SERVICE_ROLE_KEY`：只保存在 Gateway 服务器的凭证，不可进入客户端或 Git。

示例（不要提交真实 token）：

```json
{
  "tester-01": {"token": "生成的随机长字符串", "daily_calls": 50, "daily_tokens": 300000},
  "tester-02": {"token": "另一条随机长字符串", "daily_calls": 50, "daily_tokens": 300000}
}
```

新内测用户在 Caddie 中输入一次邀请码，Gateway 动态签发设备凭证。凭证保存在
macOS Keychain，用户看不到 DeepSeek Key 或 Gateway Token。

- Base URL：`https://你的-gateway-域名/v1`
- Model：`deepseek-v4-flash`

Gateway 保存邮箱、称呼、内测批次、设备关系和额度统计，不保存求职资料、AI 请求正文或回复正文。粗粒度行为事件只携带账户、设备和批次 ID，不携带邮箱和称呼。

## 内测数据台

1. 在 Supabase SQL Editor 执行 `supabase/telemetry_schema.sql`。
2. 将 `SUPABASE_URL` 和 `SUPABASE_SERVICE_ROLE_KEY` 写入 `/opt/caddie-gateway-secrets.env`。
3. 重新执行 `sudo bash gateway/deploy-tencent.sh`。
4. 打开 `https://43-128-7-135.sslip.io/gateway/admin`，输入 `/opt/caddie-gateway/admin-credential.txt` 中的管理 Token。

数据台展示用户活跃、功能使用、AI 与外部 Agent 粗粒度事件，不展示简历、JD、提示词、AI 对话或文件路径。

## 官网联动

官网使用管理凭证调用 `POST /admin/v1/invites/batch`，并在请求中传入
`application_public_id`。用户兑换后，Gateway 会调用官网的激活回调；如果官网临时
不可用，兑换仍然成功，管理员可用下面的接口补做状态核对：

```text
GET /admin/v1/invites/by-application/{application_public_id}
```

腾讯云部署前，在服务器创建仅 root 可读的密钥文件：

```bash
sudo install -m 600 /dev/null /opt/caddie-gateway-secrets.env
sudo sh -c 'printf "DEEPSEEK_API_KEY=你的密钥\\nCADDIE_WEBSITE_CALLBACK_URL=https://官网域名/api/internal/activations\\nCADDIE_GATEWAY_CALLBACK_TOKEN=随机长字符串\\n" > /opt/caddie-gateway-secrets.env'
sudo bash gateway/deploy-tencent.sh
```

部署脚本首次运行会保留两份旧版兼容凭证，并在
`/opt/caddie-gateway/admin-credential.txt` 生成仅 root 可读的后台管理凭证。
公网只提供 `/gateway/*`，其他路径返回 404；
完整 Caddie、SQLite、Git 历史、MCP 和外部 Agent 始终运行在测试者 Mac 本地。
