# 离线代码测试

## 运行

要求 Python 3.12+。基础档仅使用标准库，不安装依赖，不读取真实配置，不调用模型。

```powershell
python -B tests/offline/run.py
python -B tests/offline/run.py --full
```

项目独立环境安装和运行（不激活环境也可）：

```powershell
python -m venv .venv-offline
.\.venv-offline\Scripts\python.exe -m pip install -r tests/offline/requirements.txt
.\.venv-offline\Scripts\python.exe -B tests/offline/run.py --full
```

安装阶段联网下载软件包；测试阶段禁止联网，不需要 API Key，不下载模型权重。`.venv-offline/` 已加入 Git 忽略。

2026-11-04 已在 `.venv-offline`（Windows / Python 3.12）安装依赖并实跑完整档：35 项通过、0 失败、0 错误、0 跳过、隔离违规为 0，数据哈希未改变。精确安装版本保存于 `requirements-lock.txt`（该快照含 Windows 专用包，跨平台安装使用 `requirements.txt`）。集成档另含 API/状态用例，当前合计48项，见下文报告。

测试子进程关闭 urllib3 导入时的 IPv6 绑定探测；网络连接和监听守卫仍然启用。测试替身返回 JSON 文本，遵循真实 AgentScope Msg 的内容协议。

入口可从任意工作目录以绝对路径运行。不要用原有 `tests/eval/run_eval.py` 替代，它包含在线评测逻辑。不要直接发现整个 tests 目录来运行本套隔离测试。

`--full` 需要项目环境已安装 AgentScope、PyYAML、OR-Tools 和 httpx，依赖缺失返回非零并在报告标记不可运行，不会自动下载安装，也不会把缺失测试算作通过。

## 实现范围

2026-11-04 接口/状态专项：

```powershell
.\.venv-offline\Scripts\python.exe -B tests/offline/run.py --integration --report-dir tests/results/integration-latest
```

测试涵盖输入边界、敏感路由、会话隔离与并发、幂等重放、异常释放、JSON历史回放/删除、重规划复用边界和偏好缓存一致性。报告会写入指定输出目录。CI配置已写入，尚未在GitHub实际运行。

2026-11-04 又增加敏感路由在模型未配置/熔断器打开时的接口回归用例。该用例先复现了真实问题，修复后继续加入规划 agent 三阶段链路用例；最新48项全部通过，见 `../results/recheck-fixed-20261104/summary.md`；修复前失败报告保留在 `../results/recheck-red-20261104/`。

API测试经过真实路由、生命周期、意图解析、dispatcher、声明式runner、JSON记忆和历史查询；固定模型替身、禁用外部存储连接、注入临时测试配置，不替换整个chat业务函数。追问续跑验证上下文传递；不声称已完成真实模型规划质量评估。

- `test_core.py`：21 个测试方法，覆盖 1604 条数据完整性、契约、行程/费用/冲突校验、有界修复、幂等与 TTL、JSON 记忆往返、会话历史过滤、滑动窗口、熔断与重试。
- `test_full.py`：14 个测试方法，覆盖真实 SkillDispatcher 的门控、consumes、快速路径、复用、并行依赖、错误隔离、追踪会话过滤；真实 OR-Tools 小型约束求解与 plan-trip 提取/求解/叙述/复核链路；真实 durable HTTP 客户端搭配内存 MockTransport。
- `test_integration.py`：13 个测试方法，覆盖 API 输入/输出、模型缺失、敏感路由、历史持久化、幂等、并发会话和缓存一致性。当前集成总数为48项。
- 网络、越界写入、读取真实配置的三个负向自检在独立进程执行，实际触发审计守卫，并故意捕获异常。只有违规仍导致失败退出，正式代码测试才开始。

尚未验证真实模型回答质量、真实 PostgreSQL/Redis 服务读写和 durable runtime 进程中断恢复。ChinaTravel 的1604条记录目前只做数据完整性校验；100条 `cases.json` 属于需要模型执行的评测集，本次没有运行。

## 隔离与产物

每次启动独立子进程，临时目录形如 `shanglv-offline-...`。所有 JSON 记忆和报告写到该目录；结束时由 TemporaryDirectory 清理每个用例的临时文件，保留该次报告及负向自检报告用于排查，终端打印绝对路径。

子进程安装 Python audit hook，拒绝网络连接、监听、DNS、子进程、正式配置/存储读取及临时根目录外的写操作。异常被业务捕获后，违规记录仍使整次运行失败。测试前后验证测试数据 SHA256。未建立真实数据库连接，不访问正式 SQLite，不清理真实数据。

该守卫用于防止受控代码测试误用真实资源，不是对不可信原生扩展的操作系统安全沙箱。它不能保证拦截原生库绕过 Python 审计直接执行的所有系统调用；本套不使用真实数据库或原生网络客户端。已有文件描述符的直接写入也不在路径审计范围内，仅保留测试输出句柄。

协程测试使用不创建 socketpair 的单线程事件循环；只支持事件、计时器和内存 transport，线程唤醒和文件/socket IO 明确报错。这避免 Windows 的 asyncio 自唤醒管道建立 loopback 网络。

## 数据修正说明

离线完整性测试确认 `human1000.csv` 只有 uid 和自然语言需求，没有结构化城市、人数和天数字段。导入时生成的 0 不能视为参考答案，已将 `human1000.json` 的 `reference_entities` 修正为空对象。原始 CSV 和中文需求未修改，不执行上游 DSL，也不从需求文本猜测标准答案。

## 报告口径

`report.json` 分别记录测试方法数量、失败/错误/跳过、隔离违规、数据是否改变和完整档依赖缺失项。1604 条只是数据完整性校验数量，不是 1604 条规划通过；固定替身输出的测试不衡量模型理解能力或官方 ChinaTravel 成绩。
