# 已复现缺陷（2026-11-04）

状态：BUG-001～005 已修复并通过离线回归；下文保留修复前的复现记录。修复前报告位于 `tests/results/integration-latest`，修复后报告位于 `tests/results/fixed-latest`。

2026-11-04 修复记录：会话pipeline独立但共享同用户权威长期存储，同会话请求串行；in-flight用finally收尾；拒绝空白消息；重规划用实体与偏好快照保守判定复用，缺少快照则重算；偏好读缓存暂时停用，直接读取权威存储。代价是偏好读取增加数据库访问、输入变化时重算更多技能。未承诺跨进程JSON写一致性或分布式严格一次。

修复后44项全部通过，0错误、0跳过、0隔离违规，源数据未改变。补充验证了A/B各自历史和trace归属，以及实体未变可复用、日期变化不复用。回归命令：

```powershell
.\.venv-offline\Scripts\python.exe -B tests/offline/run.py --integration --report-dir tests/results/fixed-latest
```

以下命令会覆盖旧报告，仅在需要重新采样时使用：

```powershell
.\.venv-offline\Scripts\python.exe -B tests/offline/run.py --integration --report-dir tests/results/integration-latest
```

固定替身模拟模型和依赖，接口实际经过 FastAPI lifespan、Pydantic、意图解析、dispatcher、runner、JSON记忆和历史读取；无真实模型、PG、Redis连接。测试失败返回非零，未标记 xfail。

## BUG-001：会话交错导致响应归属错误（高）

测试：`ApiTests.test_same_user_sessions_interleave_without_context_leak`。

前置：同用户，A/B两会话。A执行技能时以 Event 暂停，B完整执行后释放A。

预期：A响应 session_id 为A，写入A历史。实际：A响应 session_id 为B。首个断言已失败，后续历史断言尚未执行，因此不能声称已验证历史错写范围。

根因定位：server.get_pipeline 按用户共享可变 MemoryManager、dispatcher，switch_session 改变会话绑定；A等待期间B修改共享对象。建议评估请求/会话隔离或按用户串行保护，需同时处理长期JSON多实例写入一致性，不能只修改响应字段。

## BUG-002：异常后请求计数泄漏（中）

测试：`ApiTests.test_pipeline_exception_releases_key_and_gauge`。

步骤：通过 /chat 注入 dispatcher 执行异常。预期502，幂等键释放，in-flight恢复基线。实际502和键释放均符合，但计数从0变1后未回落。

根因定位：server._chat_execute 多条异常出口不经过 IN_FLIGHT.dec。建议统一 finally 管理，移除重复递减点，增加成功/敏感/取消/异常分支回归。

## BUG-003：纯空白内容进入业务（中，待业务规则确认）

测试：`ApiTests.test_whitespace_should_be_rejected`。

输入：POST /chat，message为三个空格。目标规则：422且无模型调用。实际：200且执行意图和技能。

根因定位：ChatRequest只限制字符串长度，没有空白校验。是否允许首尾空白、保留原始文本还是规范化，应先确认规则；不能把需求决定包装成历史实现承诺。

## BUG-004：换目的地仍复用旧差标（高）

测试：`ConsistencyTests.test_changed_destination_must_not_reuse_old_city_data`。

前置：缓存成功的杭州 budget_check 结果。新计划replan=true，目的地北京，P1差标+P2规划。

预期：旧杭州结果不能复用。实际：plan.reused中保留杭州差标。

根因定位：_apply_replan只检查优先级、名称和成功状态，没有缓存输入依赖。该测试验证计划阶段，不声称已观察真实模型生成北京错误费用。建议保存结果输入指纹，并按城市/日期等依赖判定失效。

## BUG-005：删缓存后写库可残留旧缓存（高）

测试：`ConsistencyTests.test_preference_write_must_not_leave_stale_cache`。

步骤：如家偏好已缓存→更新前删缓存→模拟并发读旧存储并回填→写万豪成功→再次读取。

预期万豪；实际存储是万豪但缓存仍返回如家。

根因定位：_CachedLongTermProxy.save_preference先失效后写库，存在读回填窗口。替身证明业务操作顺序问题，不是实测Redis/PG事务。修复需明确一致性要求，不能声称简单写后删除即可解决所有并发读写竞态。

## 范围与证据

新设计共10项集成/一致性测试，原有33项保留。接口断言和替身操作均可在源码复查。JSON/JUnit报告记录各失败完整堆栈。

尚未完成：真实进程恢复、鉴权/越权、PG删除路径、请求取消、TTL在途过期等，不得用现有通过结果替代。
