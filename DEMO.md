# Caddie 虚拟测试版

测试版复用当前 Caddie 的全部代码和界面，但使用独立数据库，不会读取或修改
`~/.caddie` 中的真实数据。

## 启动

```bash
./scripts/caddie-demo.sh
```

默认地址为 `http://127.0.0.1:8877`，数据位于项目内的 `.demo-data/`。

虚拟求职者为「林知夏」：复旦大学管理科学硕士，目标方向为 AI 产品、策略产品和
商业分析。演示库包含教育背景、5 段经历、8 个项目、7 条求职线、投递、机会库、
日历、简历版本、来源证据、知识库、岗位差距、衍生话术、聊天、Agent 运行记录，
以及带逐字稿、问题、回答、评分、预测和结果校准的面试复盘链路。

## 重建数据

关闭测试版后执行：

```bash
./.venv/bin/python scripts/create_demo_workspace.py \
  --data-dir .demo-data \
  --reset
```

可通过环境变量改目录和端口：

```bash
CADDIE_DEMO_DATA_DIR=/tmp/caddie-demo CADDIE_DEMO_PORT=8899 \
  ./scripts/caddie-demo.sh
```

`create_demo_workspace.py` 会拒绝把 `~/.caddie` 作为目标，避免误覆盖真实工作区。
