# 本地聊天界面

已有简洁的聊天主界面：左侧历史会话，右侧消息与输入框，支持新会话、历史恢复、删除及行程卡片。

启动后端（需先安装根目录 requirements.txt 中的应用依赖并配置模型；离线测试环境只包含测试依赖）：

```powershell
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

默认启用 pi-durable。只启动 Python 和前端时，请先在后端终端执行 `$env:PI_DURABLE_ENABLED = "0"`，再运行上面的命令，使用 Python 本地调度。启用 pi-durable 时，任务提交异常会提示重试，网页的「重试这条请求」会复用原请求 ID。

另开终端，从 web-next 目录启动前端：

```powershell
pnpm install
pnpm dev
```

访问 http://localhost:3000 。前端 /api 请求默认代理到 localhost:8000，可通过 BACKEND_URL 修改。

高德 MCP 接入使用官方 Streamable HTTP 服务。在启动 Python 的同一个终端设置密钥，随后启动或重启后端：

```powershell
$env:AMAP_MAPS_API_KEY = Read-Host '输入高德地图 MCP Key'
$env:AMAP_MCP_ENABLED = '1'
$env:AMAP_TRANSPORT = 'driving' # 可改为 walking
```

可问“杭州西湖在哪里”“杭州灵隐寺附近的餐厅”“杭州西湖到灵隐寺开车多久”。规划行程时会查询候选地点和有向路线耗时，另加10分钟缓冲再排程。默认候选最多10项，地图查询总时限90秒。缺密钥、同名地点或查询超时会显示交通估算提示。地图结果只代表查询时的路线，未来出行需再确认。

接入依据为[高德官方快速接入文档](https://lbs.amap.com/api/mcp-server/gettingstarted)。密钥只在后端配置，不放入浏览器或提交到仓库。

历史聊天保存在后端，不依赖当前网页是否打开。PostgreSQL 模式写数据库；未连接数据库时写根目录 data/memory/{user_id}.json。JSON 模式也支持侧栏历史列表、重启后恢复和删除单个会话。勿清理该目录；浏览器记住上次使用的用户ID。当前尚未实现账户认证，仅适合本地个人使用。

删除仅删除该会话的聊天与会话缓存，不删除用户偏好或行程历史。删除失败会显示提示，不再假装删除成功。多进程同时写 JSON 文件不在当前保证范围内。

验证：44项离线代码测试通过（含文件历史保存、重建状态恢复、A/B会话隔离删除）；前端通过 TypeScript 检查和生产构建。未调用真实模型进行浏览器端到端对话测试。
