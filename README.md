# 差旅助手 2.0

面向个人用户的差旅助手，支持行程规划、差旅知识问答、天气与地点查询、偏好管理和历史行程。

- `server.py` 提供 FastAPI 接口，`web-next/` 提供 Next.js 前端。
- `agents/` 负责意图识别和技能调度，`skills/` 包含技能实现。行程规划使用 OR-Tools 排程。
- `services/pi-durable/` 提供可选的任务持久化执行服务，保存任务检查点并支持恢复。

运行前通过环境变量配置 `AIGC_BASE_URL`、`AIGC_APP_ID` 和 `FILL_THOUGHT_MODEL`。准备好 Python 和 Node.js 依赖后，分别在两个终端启动后端与前端，访问 `http://localhost:3000`。详细步骤见[项目运行说明](文档和视频/如何运行.md)和[前端运行说明](web-next/LOCAL_USAGE.md)。

项目包含 [ChinaTravel 中文知识表和索引重建说明](skills/ask-question/data/chinatravel/README.md)。克隆后可用导入脚本从原始表重建关键词索引。资料中的价格、开放时间是静态样例。API 密钥和运行数据库需要在本地配置。

离线回归测试不需要模型 API 或生产数据：

```sh
python -m pip install -r tests/offline/requirements.txt
python -B tests/offline/run.py --full --integration
```

GitHub Actions 使用隔离环境运行离线测试。服务运行需要单独启动前端、后端以及所需的可选服务。
