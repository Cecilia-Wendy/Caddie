# Caddie 邀请码内测平台与托管 AI API 方案

## 1. 目标体验

内测用户的完整路径只有三步：安装 Caddie、输入邀请码、开始使用。用户不申请模型 API、不充值、不填写 Key，也看不到 DeepSeek Key 或 Gateway 设备凭证。

正式版继续保留自有 API、Ollama、MCP 和外部 Agent。邀请码体系仅存在于 Hosted Alpha，不修改正式版产品策略。

## 2. 系统边界

```text
招募网站                 用户 Mac                     腾讯云 Gateway
申请/候补/发码    ->    邀请码激活              ->   账号与设备凭证
群二维码与反馈          本地 SQLite / Git / MCP       双层额度与 AI 代理
下载最新版本            凭证存 macOS Keychain        不保存 Prompt 和回复
```

招募网站保存申请资料和运营状态；Gateway 保存邀请码哈希、匿名账号、设备凭证哈希、额度和粗粒度用量；Caddie 求职资料始终保存在用户本机。

## 3. 默认额度

- 激活后有效期：14 天。
- 总 Token：2,000,000。
- 每日 Token：300,000。
- 每日调用：30 次。
- 今日或总额度低于 20% 时客户端提醒。
- 达到任一上限后 Gateway 返回 429，不继续调用 DeepSeek。
- 管理员可以暂停、恢复、撤销、续期或增加额度。

## 4. 邀请码与凭证安全

- 邀请码随机生成、只能兑换一次、默认 48 小时失效。
- 数据库只保存邀请码 SHA-256，不保存邀请码明文。
- 激活时绑定匿名账号和一台设备。
- 设备凭证只在激活响应中返回一次，Gateway 只保存凭证 SHA-256。
- Caddie 把设备凭证写入 macOS Keychain，`config.json` 只保留 `__caddie_keychain__` 标记。
- 安装包只包含公开 Gateway URL，不包含长期凭证、管理令牌或 DeepSeek Key。
- 网站服务端持有管理令牌；浏览器前端绝不持有管理令牌。

## 5. Gateway API

### 客户端公开接口

`POST /gateway/v1/activate`

```json
{
  "invite_code": "ABCD-EFGH-IJKL",
  "device_id": "mac_匿名随机ID",
  "device_name": "Wendy's Mac"
}
```

成功时返回一次性可见的 `credential`、账号 ID、到期时间和额度。邀请码重复使用返回 409，过期返回 410。

`GET /gateway/v1/usage`

使用 `Authorization: Bearer <设备凭证>`，返回今日用量、累计用量、双层上限、到期时间和账号状态。

`POST /gateway/v1/chat/completions`

兼容现有 Caddie AI 调用。后续逐步增加 `/tasks/resume-tailor`、`/tasks/jd-gap`、`/tasks/interview-review` 等受控任务接口，届时通用聊天代理可仅保留兼容用途。

### 运营后台接口

所有接口使用 `Authorization: Bearer <CADDIE_GATEWAY_ADMIN_TOKEN>`：

- `POST /gateway/admin/v1/invites/batch`：按批次生成 1–100 个邀请码。
- `GET /gateway/admin/v1/accounts`：查看账号、设备数、累计调用和 Token。
- `POST /gateway/admin/v1/accounts/{id}/status`：暂停、恢复或撤销。
- `POST /gateway/admin/v1/accounts/{id}/quota`：续期或调整三类额度。

网站后台每天发 5 个码时，请调用：

```json
{
  "count": 5,
  "batch_name": "2026-08-03-day-02",
  "valid_hours": 48,
  "trial_days": 14,
  "total_tokens": 2000000,
  "daily_tokens": 300000,
  "daily_calls": 30
}
```

## 6. 招募网站数据模型

建议网站独立数据库保存：

- `applications`：邮箱、使用目的、Mac 情况、申请时间、状态。
- `release_batches`：每日名额、发放时间、暂停状态。
- `invite_deliveries`：申请 ID、Gateway 邀请码、48 小时到期时间、激活状态。
- `community_membership`：是否进群、群批次、反馈次数。
- `feedback_tickets`：问题描述、预期、实际、截图、版本、严重程度、处理状态。

网站不要保存设备凭证、DeepSeek Key、Caddie 本地资料或 Prompt。

## 7. 100 人递进放量

建议 5 -> 10 -> 20 -> 40 -> 100。每批放量前检查：激活成功率、AI 成功率、更新成功率、阻断问题数、日均成本和反馈处理能力。邀请码自动发放应有全局暂停开关，出现数据或鉴权事故时立即停止下一批。

## 8. 部署迁移

1. 先备份 `/opt/caddie-gateway-data/gateway.sqlite3`。
2. 在 secrets 文件中增加 `CADDIE_GATEWAY_ADMIN_TOKEN`，或由部署脚本首次生成。
3. 部署新版 Gateway；SQLite 会增量创建邀请码、账号和设备表。
4. 旧 `CADDIE_GATEWAY_TESTERS_JSON` 暂时保留，保证已安装测试者继续使用。
5. 网站后台接入管理接口后，先生成 1 个邀请码完成端到端验证。
6. 验证 Keychain、额度、暂停和到期逻辑后，再按每天 5 人放量。

## 9. 验收标准

- 安装包中搜索不到 DeepSeek Key、设备凭证和管理令牌。
- 同一邀请码第二次兑换失败。
- 数据库中不存在邀请码和设备凭证明文。
- 重启 Caddie 后无需再次输入邀请码。
- 暂停账号后下一次 AI 调用立即失败。
- 今日和体验期额度均可独立限制。
- 客户端低于 20% 显示提醒，用完后明确说明原因。
- 旧测试 Token 在迁移期继续有效。
