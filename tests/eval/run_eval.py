"""
评测运行器（评测体系：评测驱动开发）

跑 tests/eval/cases.json 的评测集，输出四个维度的指标（参照三个参考项目的评测体系）：
  1. 意图路由准确率     —— 期望 agent 是否被调度（学图3 的 F1 思路，取命中率）
  2. 端到端通过率 Pass@1 —— 答案含验收要点（must_contain）且无报错
  3. 延迟 P50 / P95     —— 每 case 墙钟计时（意图识别 + 编排全链路）
  4. 敏感路由拦截率     —— escalation 类的确定性路由命中

用法：
  venv/bin/python tests/eval/run_eval.py                # 全量（先跑内置单测）（~30 case，约 30-60 分钟）
  venv/bin/python tests/eval/run_eval.py --only rag    # 只跑 id 前缀匹配的
  venv/bin/python tests/eval/run_eval.py --sample 5    # 每意图抽 5 条快速冒烟

报告输出 tests/results/eval_<timestamp>.md，与历史报告对比看回归。
"""
import sys
import json
import time
import asyncio
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import logging
logging.basicConfig(level=logging.WARNING)


async def run_eval():
    from config import LLM_CONFIG
    from agentscope.model import OpenAIChatModel
    from agentscope.message import Msg
    from context.memory_manager import MemoryManager
    from agents.intention_agent import IntentionAgent
    from agents.skill_dispatcher import SkillDispatcher
    from agents.lazy_agent_registry import LazyAgentRegistry
    from utils.sensitive_router import check_sensitive_request

    cases = json.loads((project_root / "tests/eval/cases.json").read_text(encoding="utf-8"))["cases"]

    print(f"评测集：{len(cases)} 条")
    ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []

    model = OpenAIChatModel(
        model_name=LLM_CONFIG["model_name"], api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"]},
        generate_kwargs={"temperature": 0.7, "max_tokens": 16384},
    )

    # pre_state 预置：fresh_user（无历史）/ with_history（有行程+偏好）/ with_conflict（有未来冲突行程）
    # 每个独立 user 避免互相污染
    state_users = {}
    def get_user_for(case):
        state = case.get("pre_state", "default")
        if state not in state_users:
            uid = f"eval_{state}_{int(time.time()) % 100000}"
            mm = MemoryManager(user_id=uid, session_id=f"eval_{state}")
            if state == "with_history":
                mm.long_term.save_preference("hotel_brands", ["如家"])
                mm.long_term.save_preference("airlines", ["东航"])
                mm.long_term.save_trip_history({"origin": "北京", "destination": "杭州",
                                                "start_date": "2026-06-11", "end_date": "2026-06-13",
                                                "purpose": "出差"})
                mm.long_term.save_trip_history({"origin": "北京", "destination": "上海",
                                                "start_date": "2026-08-20", "end_date": "2026-08-22",
                                                "purpose": "出差"})
            elif state == "with_conflict":
                mm.long_term.save_trip_history({"origin": "北京", "destination": "深圳",
                                                "start_date": "2027-06-10", "end_date": "2027-06-12",
                                                "purpose": "出差"})
            state_users[state] = (uid, mm, LazyAgentRegistry(model, {}, mm))
        return state_users[state]

    intention = IntentionAgent(name="IntentionAgent", model=model)

    # 编排器全程复用一个（构造会建 PG 追踪连接，逐 case 重建会泄漏连接 + 重读全部 SKILL.md）；
    # 每 case 只换记忆/注册表归属，并清空重规划缓存（防止跨 user 串结果）
    orch = SkillDispatcher(name="SkillDispatcher")

    for i, case in enumerate(cases, 1):
        q = case["query"]
        print(f"[{i}/{len(cases)}] {case['id']}: {q[:30]}...", flush=True)
        t0 = time.monotonic()

        uid, mm, registry = get_user_for(case)
        orch.agent_registry = registry
        orch.memory_manager = mm
        orch._last_results = {}
        orch.set_trace_context(uid, mm.session_id, q)

        agents_routed, display, error = [], "", None
        try:
            # 敏感前置路由
            sens = check_sensitive_request(q)
            if sens:
                agents_routed, display = ["escalation"], sens["response"]
            else:
                ir = await intention.reply([Msg(name="user", content=q, role="user")])
                orch_data = json.loads(ir.content)
                orch.tracer.record(user_id=uid, session_id=mm.session_id,
                                   agent_name="intention_recognition", status="success",
                                   duration_ms=int((time.monotonic() - t0) * 1000), priority=0)
                orr = await orch.reply(ir)
                rd = json.loads(orr.content)
                agents_routed = [r.get("agent_name") for r in rd.get("results", [])]
                if rd.get("status") == "needs_input":
                    agents_routed = ["needs_input"]
                    display = rd.get("message", "")
                else:
                    from io import StringIO
                    from rich.console import Console
                    from cli import ShanglvCLI
                    cap = StringIO()
                    c = Console(file=cap, force_terminal=False, no_color=True, width=100)
                    cli = ShanglvCLI()
                    cli.console = c
                    cli._display_results(rd)
                    display = cap.getvalue()
        except Exception as e:
            error = str(e)[:120]

        dur = time.monotonic() - t0

        # ---- 判定 ----
        # 1. 路由判定
        raw_exp = case.get("expect_agents") or case.get("expect_agent") or []
        exp = list(raw_exp) if isinstance(raw_exp, (list, tuple)) else [raw_exp]
        route_ok = bool(exp) and all(e in agents_routed for e in exp)
        # 2. 内容判定
        text = display or ""
        content_ok = all(k in text for k in case.get("must_contain", [])) if case.get("must_contain") else True
        content_ok = content_ok and ("请求失败" not in text and "执行失败" not in text)
        passed = route_ok and content_ok and error is None

        results.append({
            "id": case["id"], "query": q, "intent": case["intent"],
            "expect": exp, "routed": agents_routed,
            "route_ok": route_ok, "content_ok": content_ok,
            "passed": passed, "duration_s": round(dur, 1),
            "error": error,
            "difficulty": case.get("difficulty", "medium"),
            "rubric_points": case.get("rubric_points"),
            "answer": text,
            "display_head": text[:150].replace("\n", " "),
        })
        mark = "✓" if passed else "✗"
        print(f"    {mark} route={route_ok} content={content_ok} {dur:.1f}s routed={agents_routed}", flush=True)

        # 清理该 case 的会话痕迹（保留 pre_state 的底数据）
        try:
            mm.short_term.clear()
        except Exception:
            pass

    # ---- 多轮场景（同一会话内连续对话，验证跨轮行为）----
    multi_turn_cases = json.loads((project_root / "tests/eval/cases.json").read_text(encoding="utf-8")).get("multi_turn", [])
    mt_results = []
    if multi_turn_cases:
        print(f"\n多轮场景：{len(multi_turn_cases)} 组")
        from io import StringIO
        from rich.console import Console
        from cli import ShanglvCLI
        for mc in multi_turn_cases:
            uid = f"eval_mt_{int(time.time()) % 100000}"
            mm = MemoryManager(user_id=uid, session_id=f"mt_{mc['id']}")
            orch = SkillDispatcher(name="SkillDispatcher",
                                      agent_registry=LazyAgentRegistry(model, {}, mm),
                                      memory_manager=mm)
            turns_ok, turn_details = True, []
            for t_i, turn in enumerate(mc["turns"], 1):
                q = turn["query"]
                t0 = time.monotonic()
                try:
                    sens = check_sensitive_request(q)
                    if sens:
                        routed, display = ["escalation"], sens["response"]
                    else:
                        # 与 CLI/server 同口径：最近 5 轮短期记忆作为意图识别的上下文
                        # （跨轮指代消解、缺要素补充都依赖这里；不注入则轮 2 是无头话）
                        recent = mm.short_term.get_recent_context(n_turns=5)
                        ctx_msgs = [Msg(name="user", content=q, role="user")]
                        if recent:
                            ctx_msgs = ([Msg(name=m["role"], content=m["content"], role=m["role"])
                                        for m in recent] + ctx_msgs)
                        ir = await intention.reply(ctx_msgs)
                        orr = await orch.reply(ir)
                        rd = json.loads(orr.content)
                        routed = [r.get("agent_name") for r in rd.get("results", [])]
                        if rd.get("status") == "needs_input":
                            routed = ["needs_input"]
                            display = rd.get("message", "")
                        else:
                            cap = StringIO()
                            cc = Console(file=cap, force_terminal=False, no_color=True, width=100)
                            cli = ShanglvCLI(); cli.console = cc
                            cli._display_results(rd)
                            display = cap.getvalue()
                    raw_exp = turn.get("expect_agents") or turn.get("expect_agent") or []
                    exp = list(raw_exp) if isinstance(raw_exp, (list, tuple)) else [raw_exp]
                    route_ok = bool(exp) and all(e in routed for e in exp)
                    content_ok = all(k in display for k in turn.get("must_contain", [])) if turn.get("must_contain") else True
                    ok = route_ok and content_ok
                    turns_ok = turns_ok and ok
                    # 落会话记忆：轮 2 的指代消解/要素补充依赖上面的历史注入
                    mm.add_message("user", q)
                    mm.add_message("assistant", display or "")
                    turn_details.append(f"轮{t_i} {'✓' if ok else '✗'} routed={routed} {time.monotonic()-t0:.1f}s")
                except Exception as e:
                    turns_ok = False
                    turn_details.append(f"轮{t_i} ✗ 异常 {str(e)[:60]}")
            mt_results.append({"id": mc["id"], "desc": mc["description"], "passed": turns_ok,
                               "turns": turn_details})
            print(f"  {'✓' if turns_ok else '✗'} {mc['id']} {mc['description']}")
            for td in turn_details:
                print(f"      {td}")
            try:
                mm.long_term.delete_all(); mm.long_term.close()
            except Exception:
                pass

    # ---- 会话隔离（同会话名双用户不串数据）----
    iso_ok = True
    if True:
        print("\n会话隔离检查")
        try:
            import redis as redis_lib
            from config import REDIS_CONFIG
            from context.short_term_memory import ShortTermMemory
            # 幂等：先清残留 key（上轮评测的同名 key 会让 lrange 多出旧消息误判）
            _r = redis_lib.Redis(port=REDIS_CONFIG.get("port", 6380), decode_responses=True)
            _r.delete("shanglv:stm:eval_iso_u1:iso_s", "shanglv:stm:eval_iso_u2:iso_s")
            m1 = ShortTermMemory(max_turns=3, session_id="iso_s", user_id="eval_iso_u1")
            m1.add_message("user", "alice消息")
            m2 = ShortTermMemory(max_turns=3, session_id="iso_s", user_id="eval_iso_u2")
            m2.add_message("user", "bob消息")
            ctx1 = [json.loads(x)["content"] for x in redis_lib.Redis(
                port=REDIS_CONFIG.get("port", 6380), decode_responses=True
            ).lrange("shanglv:stm:eval_iso_u1:iso_s", 0, -1)]
            iso_ok = ctx1 == ["alice消息"]
            print(f"  {'✓' if iso_ok else '✗'} 同会话名双用户短期记忆隔离")
        except Exception as e:
            print(f"  （跳过：{str(e)[:60]}）")

    # ---- Rubric 评测（LLM-as-Judge，多准则语义评分；--skip-rubric 可关） ----
    rubric_ok = True
    rubric_scores = []
    if results:
        print(f"\nRubric 评测（LLM Judge × {len(results)} 条）")
        try:
            from rubrics import judge_case, JudgeCache, _load_judge_model
            cache = JudgeCache(Path(__file__).parent.parent / "results" / ".judge_cache.jsonl")
            judge_model = _load_judge_model()
            hit = 0
            for r in results:
                jr = await judge_case(r["query"], r["answer"], r["intent"],
                                      r.get("rubric_points"), judge_model, cache)
                r["rubric"] = jr
                if jr.get("from_cache"):
                    hit += 1
                if jr.get("score") is not None:
                    rubric_scores.append(jr["score"])
            avg = sum(rubric_scores) / len(rubric_scores) if rubric_scores else 0
            print(f"  Rubric 平均分: {avg:.3f}（可判定 {len(rubric_scores)}/{len(results)}，缓存命中 {hit}）")
        except Exception as e:
            rubric_ok = False
            print(f"  ✗ Rubric 评测异常 {str(e)[:80]}")

    # ---- 回流管线（结构化结果分级落盘；--skip-backflow 可关） ----
    backflow_stats = None
    if results:
        try:
            from backflow import build_backflow_records, write_backflow, LABELS
            records = build_backflow_records(results)
            bf_path = Path(__file__).parent.parent / "results" / f"backflow_{ts_tag}.jsonl"
            backflow_stats = write_backflow(records, bf_path)
            print(f"\n回流管线：{len(records)} 条分级落盘 {bf_path.name}")
            print("  " + " · ".join(f"{k} {v}" for k, v in backflow_stats.items()))
        except Exception as e:
            print(f"\n回流管线异常（不影响评测结论）: {str(e)[:80]}")

    # ---- durable 链路 E2E（M8）：验证 /chat 主路径真实走 pi-durable 任务树 ----
    durable_ok = False
    durable_note = ""
    print("\ndurable 链路 E2E（/chat → pi-durable 任务树）")
    try:
        import httpx
        from config import PI_DURABLE_CONFIG as _pdc
        base = _pdc.get("base_url", "http://127.0.0.1:8788")
        # 1. pi-durable 存活 + 令牌互通
        health = httpx.get(f"{base}/health", timeout=3)
        alive = health.status_code == 200
        # 2. 走 API 的 /chat 完成一轮（真实 LLM）
        chat_ok, task_kinds = False, []
        if alive:
            api = httpx.post("http://127.0.0.1:8000/chat",
                             json={"message": "北京的差旅住宿预算标准是多少",
                                   "user_id": "eval_durable", "session_id": "eval_durable_s"},
                             timeout=180)
            chat_ok = api.status_code == 200 and api.json().get("agents_called")
            # 3. SQLite 任务表：该轮应有 pi.generation（意图）+ shanglv.skill.gen（skill 生成）+ pipeline
            if chat_ok:
                import sqlite3 as _sq
                db = _sq.connect("data/pi-durable/session.sqlite")
                # 注：pi-durable 的 SQLite 把 kind 存为带引号的 JSON 字符串（"pi.generation"）
                rows = db.execute(
                    "SELECT kind FROM tasks WHERE kind LIKE '%shanglv.%' OR kind LIKE '%pi.generation%' "
                    "ORDER BY id DESC LIMIT 8").fetchall()
                task_kinds = [r[0] for r in rows]
                db.close()
                has_gen = any("pi.generation" in k for k in task_kinds)
                has_pipe = any("shanglv" in k for k in task_kinds)
                durable_ok = has_gen and has_pipe
                if not has_gen:
                    durable_note = "无 pi.generation——skill 生成未走 durable"
                elif not has_pipe:
                    durable_note = "无 shanglv 任务——编排未走 durable"
        print(f"  {'✓' if durable_ok else '✗'} /chat 全链路在 pi-durable 上（{', '.join(task_kinds[:4])}）{durable_note}")
        if not alive:
            print("  （pi-durable 未运行——该项失败而非跳过：评测要求主路径可用）")
    except Exception as e:
        print(f"  ✗ 异常 {str(e)[:80]}")

    # ---- 确定性校验器正反用例（质量保障层的输出守门） ----
    verifier_ok = True
    print("\n确定性校验器（坏输出必须被抓到）")
    try:
        from utils.evaluator import verify_expense_estimate, verify_conflict_check
        # 反例1：预算上限算术不符（800+690≠1000）必须 failed
        bad_cap = {"budget_cap": {"accommodation": 800, "meals": 690, "total": 1000},
                   "estimate": {"total": {"range": [1000, 2000]}}, "over_budget": True}
        v1 = verify_expense_estimate(bad_cap)
        cap_caught = v1["verdict"] == "failed" and any("算术不符" in i for i in v1["issues"])
        # 反例2：标了 no_conflict 但日期区间与历史行程重叠
        bad_conf = {"result": "no_conflict",
                    "checked_trip": {"start_date": "2027-01-01", "end_date": "2027-01-05"},
                    "trips": [{"destination": "杭州", "start_date": "2027-01-03", "end_date": "2027-01-08"}]}
        v2 = verify_conflict_check(bad_conf)
        conf_caught = v2["verdict"] == "failed" and any("重叠" in i for i in v2["issues"])
        # 正例：合规输出必须 passed（不误伤）
        good = {"budget_cap": {"accommodation": 800, "meals": 690, "total": 1490},
                "estimate": {"total": {"range": [1490, 2400]}}, "over_budget": False}
        v3 = verify_expense_estimate(good)
        no_false_alarm = v3["verdict"] == "passed"
        verifier_ok = cap_caught and conf_caught and no_false_alarm
        print(f"  {'✓' if cap_caught else '✗'} 预算算术错误被抓（800+690≠1000）")
        print(f"  {'✓' if conf_caught else '✗'} 假 no_conflict 被抓（日期重叠）")
        print(f"  {'✓' if no_false_alarm else '✗'} 合规输出不误伤")
    except Exception as e:
        verifier_ok = False
        print(f"  ✗ 异常 {str(e)[:80]}")

    # ---- 聚合报告 ----
    total = len(results)
    n_pass = sum(1 for r in results if r["passed"])
    n_route = sum(1 for r in results if r["route_ok"])
    n_content = sum(1 for r in results if r["content_ok"])
    durs = sorted(r["duration_s"] for r in results)
    p50 = durs[len(durs) // 2] if durs else 0
    p95 = durs[int(len(durs) * 0.95)] if durs else 0

    by_intent = defaultdict(lambda: [0, 0])
    for r in results:
        by_intent[r["intent"]][0] += 1
        by_intent[r["intent"]][1] += 1 if r["passed"] else 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = project_root / "tests/results" / f"eval_{ts}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("# 商旅助手评测报告\n\n")
        f.write(f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(f"## 总指标\n\n")
        f.write(f"| 指标 | 值 |\n|---|---|\n")
        f.write(f"| 意图路由准确率 | {n_route}/{total} ({n_route/total*100:.1f}%) |\n")
        f.write(f"| 端到端 Pass@1 | {n_pass}/{total} ({n_pass/total*100:.1f}%) |\n")
        f.write(f"| 内容验收通过率 | {n_content}/{total} ({n_content/total*100:.1f}%) |\n")
        f.write(f"| 延迟 P50 / P95 | {p50:.1f}s / {p95:.1f}s |\n\n")
        f.write("## 分意图\n\n| 意图 | 通过/总数 | 通过率 |\n|---|---|---|\n")
        for intent, (n, p) in sorted(by_intent.items()):
            f.write(f"| {intent} | {p}/{n} | {p/n*100:.0f}% |\n")
        f.write("\n## 失败明细\n\n")
        fails = [r for r in results if not r["passed"]]
        if not fails:
            f.write("（无）\n")
        for r in fails:
            f.write(f"### ✗ {r['id']}: {r['query']}\n\n")
            f.write(f"- 期望路由: {r['expect']} ｜ 实际路由: {r['routed']}\n")
            f.write(f"- 路由 {'✓' if r['route_ok'] else '✗'} / 内容 {'✓' if r['content_ok'] else '✗'} / {r['duration_s']}s\n")
            if r["error"]:
                f.write(f"- 错误: {r['error']}\n")
            f.write(f"- 回答头部: {r['display_head']}\n\n")

    print()
    # 报告尾段：多轮与隔离
    with open(out, "a", encoding="utf-8") as f:
        if mt_results:
            f.write("\n## 多轮场景\n\n| 场景 | 结果 |\n|---|---|\n")
            for r in mt_results:
                f.write(f"| {r['id']} {r['desc']} | {'✅' if r['passed'] else '❌'} |\n")
        f.write(f"\n## 会话隔离\n\n{'✅ 通过' if iso_ok else '❌ 未通过'}\n")
        f.write(f"\n## durable 链路 E2E\n\n{'✅ /chat 全链路走 pi-durable 任务树' if durable_ok else '❌ ' + (durable_note or '未走 durable')}\n")
        f.write(f"\n## 确定性校验器\n\n{'✅ 坏输出被抓、合规不误伤' if verifier_ok else '❌ 校验器行为异常'}\n")
        # Rubric 质量分
        if rubric_scores:
            avg = sum(rubric_scores) / len(rubric_scores)
            f.write(f"\n## Rubric 质量分（LLM Judge）\n\n平均 {avg:.3f}（可判定 {len(rubric_scores)}/{len(results)}；unverifiable 不计分母）\n\n")
            by_int = {}
            for r in results:
                sc = (r.get("rubric") or {}).get("score")
                if sc is not None:
                    by_int.setdefault(r["intent"], []).append(sc)
            if by_int:
                f.write("| 意图 | Rubric 均分 | 可判定数 |\n|---|---|---|\n")
                for k in sorted(by_int):
                    v = by_int[k]
                    f.write(f"| {k} | {sum(v)/len(v):.3f} | {len(v)} |\n")
        elif not skip_rubric:
            f.write("\n## Rubric 质量分\n\n（无可判定结果）\n")
        # 回流统计
        if backflow_stats:
            f.write("\n## 回流数据分级\n\n")
            total_bf = sum(backflow_stats.values())
            for k in ["KEEP", "REVIEW", "REWRITE"]:
                if k in backflow_stats:
                    pct = backflow_stats[k] / total_bf * 100 if total_bf else 0
                    f.write(f"- **{k}**: {backflow_stats[k]}（{pct:.0f}%）\n")

    # 结构化结果落盘（含 rubric 全字段，供外部工具消费）
    try:
        struct_path = out.parent / f"eval_{ts_tag}.json"
        struct_path.write_text(json.dumps({
            "ts": ts_tag, "total": total, "n_pass": n_pass, "n_route": n_route,
            "p50": p50, "p95": p95, "cases": results,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"（结构化结果落盘失败: {e}）")

    print("=" * 60)
    print(f"路由准确率: {n_route}/{total} ({n_route/total*100:.1f}%)")
    print(f"Pass@1:     {n_pass}/{total} ({n_pass/total*100:.1f}%)")
    print(f"P50/P95:    {p50:.1f}s / {p95:.1f}s")
    if mt_results:
        mt_pass = sum(1 for r in mt_results if r["passed"])
        print(f"多轮场景:   {mt_pass}/{len(mt_results)}")
    print(f"会话隔离:   {'✅' if iso_ok else '❌'}")
    print(f"durable链路: {'✅' if durable_ok else '❌'}")
    print(f"校验器:      {'✅' if verifier_ok else '❌'}")
    print(f"报告: {out}")
    try:
        orch.tracer.close()
    except Exception:
        pass
    return n_pass == total and all(r["passed"] for r in mt_results) and iso_ok and durable_ok and verifier_ok


if __name__ == "__main__":
    ok = asyncio.run(run_eval())
    sys.exit(0 if ok else 1)

# ================================================================ 单元测试
# （原 tests/test_migration_m.py 与 tests/test_pi_durable_m6.py 合并于此）
# 运行：venv/bin/python -m pytest tests/eval/run_eval.py
# 不依赖外部 LLM；PG/Redis 可用时走真实存储，不可用走降级路径也在测。

import asyncio
import json
import os
import sys
import time

import pytest



# ---------------------------------------------------------------- M1

class TestM1Idempotency:
    def _guard(self):
        from utils.idempotency import IdempotencyGuard
        # 显式禁 Redis → 内存模式，单测确定性强；Redis 路径由 E2E 冒烟覆盖
        return IdempotencyGuard(redis_config=False, ttl_sec=60)

    def test_fresh_then_done_replay(self):
        g = self._guard()
        key = "k1"
        async def scenario():
            st, token = await g.begin(key)
            assert st == "fresh"
            assert await g.complete(key, token, json.dumps({"answer": "hello"}))
            return await g.begin(key)
        st, cached = asyncio.run(scenario())
        assert st == "done"
        assert json.loads(cached)["answer"] == "hello"

    def test_release_allows_retry(self):
        g = self._guard()
        key = "k2"
        async def scenario():
            st, token = await g.begin(key)
            assert st == "fresh"
            assert await g.release(key, token)
            return await g.begin(key)
        st, _ = asyncio.run(scenario())
        assert st == "fresh"  # 释放后可重跑

    def test_inflight_wait_gets_same_result(self):
        g = self._guard()
        key = "k3"

        async def scenario():
            st, token = await g.begin(key)
            assert st == "fresh"

            async def waiter():
                return await g.wait_done(key, timeout_sec=2)

            async def complete_later():
                await asyncio.sleep(0.2)
                await g.complete(key, token, json.dumps({"answer": "same"}))

            results = await asyncio.gather(waiter(), complete_later())
            return results[0]  # waiter 的返回值在第一位

        result = asyncio.run(scenario())
        assert json.loads(result)["answer"] == "same"

    def test_make_key_deterministic(self):
        from utils.idempotency import make_key
        a = make_key("u", "s", "同一句话")
        b = make_key("u", "s", "同一句话")
        c = make_key("u", "s", "另一句话")
        d = make_key("u", "s", "任意", request_id="rid-1")
        e = make_key("u", "s", "完全不同", request_id="rid-1")
        assert a == b and a != c
        assert d == e  # request_id 优先，内容不同也同键


# ---------------------------------------------------------------- M2

class TestM2TracePayload:
    def _tracer(self):
        from utils.execution_tracer import ExecutionTracer
        return ExecutionTracer()

    def test_turn_payload_roundtrip(self):
        t = self._tracer()
        t.record(user_id="u_m2", session_id="sess_m2", agent_name="budget_check",
                 status="success", duration_ms=10, priority=1,
                 output_data={"x": 1}, turn_id="turn_A",
                 payload={"status": "success", "data": {"limit": 500}})
        t.record(user_id="u_m2", session_id="sess_m2", agent_name="preference",
                 status="success", duration_ms=5, priority=1,
                 output_data={"y": 2}, turn_id="turn_A",
                 payload={"status": "success", "data": {"type": "hotel_brands"}})
        t.record(user_id="u_m2", session_id="sess_m2", agent_name="plan_trip",
                 status="success", duration_ms=50, priority=2,
                 output_data={"z": 3}, turn_id="turn_B",
                 payload={"status": "success", "data": {"itinerary": {}}})
        restored = t.latest_turn_results("sess_m2")
        # 最后一个带 payload 的轮是 turn_B → 只恢复 turn_B
        assert set(restored.keys()) == {"plan_trip"}
        assert restored["plan_trip"]["data"]["itinerary"] == {}

    def test_new_instance_restores(self):
        """关键场景：重启（新 tracer 实例）后从日志恢复"""
        t1 = self._tracer()
        t1.record(user_id="u_m2", session_id="sess_r", agent_name="budget_check",
                  status="success", duration_ms=10, priority=1,
                  output_data={}, turn_id="turn_R",
                  payload={"status": "success", "data": {"limit": 400}})
        from utils.execution_tracer import ExecutionTracer
        t2 = ExecutionTracer()
        restored = t2.latest_turn_results("sess_r")
        if t2._pg_available and t1._pg_available:
            assert restored.get("budget_check", {}).get("data", {}).get("limit") == 400
        else:
            # 内存降级模式天然跨不了实例——PG 不可用时该能力声明性缺失，不算失败
            pytest.skip("PG unavailable; cross-instance restore is a PG-only feature")

    def test_dispatcher_restore(self):
        from agents.skill_dispatcher import SkillDispatcher
        d = SkillDispatcher(name="t")
        d.tracer.record(user_id="u", session_id="sess_d", agent_name="budget_check",
                        status="success", duration_ms=1, priority=1,
                        output_data={}, turn_id="t1", payload={"status": "success", "data": {}})
        d.restore_last_results("sess_d")
        assert "budget_check" in d._last_results
        # 已有缓存时不覆盖
        d._last_results = {"newer": {"status": "success"}}
        d.restore_last_results("sess_d")
        assert "newer" in d._last_results


# ---------------------------------------------------------------- M3

class TestM3ReplayDeclaration:
    def test_all_skills_declare_replay(self):
        from utils.skill_loader import SkillLoader
        infos = SkillLoader().load_skills()
        assert len(infos) >= 12, f"期望至少 12 个 skill，实际 {len(infos)}"
        for name, info in infos.items():
            replay = info.get("replay")
            assert replay in ("safe", "unsafe"), f"{name} 的 replay 声明缺失或非法: {replay!r}"

    def test_preference_is_unsafe(self):
        """写偏好的 skill 必须是 unsafe（重放会重复写入）"""
        from utils.skill_loader import SkillLoader
        infos = SkillLoader().load_skills()
        assert infos["preference"]["replay"] == "unsafe"

    def test_dispatcher_reads_replay(self):
        from agents.skill_dispatcher import SkillDispatcher
        d = SkillDispatcher(name="t")
        assert d._get_replay("preference") == "unsafe"
        assert d._get_replay("budget_check") == "safe"
        assert d._get_replay("nonexistent") is None


# ---------------------------------------------------------------- M4

class TestRubricAndBackflow:
    """Rubric 适用性 / 缓存 / Judge 解析 / 回流分级"""

    def test_rubric_applicability(self):
        sys.path.insert(0, str(Path(__file__).parent))
        from rubrics import _applicable_rubrics
        rag_ids = [r["id"] for r in _applicable_rubrics("rag_knowledge")]
        esc_ids = [r["id"] for r in _applicable_rubrics("escalation")]
        assert "FACT_GROUNDED_V1" in rag_ids and "SAFETY_COMPLIANCE_V1" in esc_ids
        assert "SAFETY_COMPLIANCE_V1" not in rag_ids and "FACT_GROUNDED_V1" not in esc_ids

    def test_judge_cache_roundtrip(self):
        import tempfile
        sys.path.insert(0, str(Path(__file__).parent))
        from rubrics import JudgeCache, _fingerprint
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            p = Path(f.name)
        c = JudgeCache(p)
        fp = _fingerprint("q", "a")
        assert c.get(fp) is None
        c.put(fp, {"criteria": [], "score": 0.5})
        assert JudgeCache(p).get(fp)["score"] == 0.5

    def test_judge_json_parse(self):
        sys.path.insert(0, str(Path(__file__).parent))
        from rubrics import _parse_json
        assert _parse_json('```json\n{"results": []}\n```') == {"results": []}
        assert _parse_json('noise {"results": [1]} tail') == {"results": [1]}

    def test_backflow_label_ladder(self):
        sys.path.insert(0, str(Path(__file__).parent))
        from backflow import label_case
        base = {"answer": "答案含 500 元", "error": None, "route_ok": True, "content_ok": True,
                "expect": ["x"], "routed": ["x"],
                "rubric": {"score": 1.0, "criteria": [{"id": "A", "status": "pass"}]}}
        assert label_case(base)["label"] == "KEEP"
        assert label_case({**base, "route_ok": False})["label"] == "REWRITE"
        assert label_case({**base, "answer": "请求失败"})["label"] == "REWRITE"
        assert label_case({**base, "rubric": {"score": None, "criteria": [{"id": "A", "status": "unverifiable"}]}})["label"] == "REVIEW"
        assert label_case({**base, "rubric": {"score": 0.5, "criteria": [{"id": "A", "status": "pass"}, {"id": "B", "status": "fail"}]}})["label"] == "REWRITE"


class TestRRFFusion:
    """混合检索 RRF 融合语义（排名制，非分数加权）"""

    def _rrf(self, *ranks):
        sys.path.insert(0, str(project_root / "skills/ask-question/script"))
        from agent import RRF_K
        return sum(1.0 / (RRF_K + r) for r in ranks if r)

    def test_both_routes_beats_single(self):
        # 双路共识（两路都排第 5）> 单路独信（只有一路排第 1）
        assert self._rrf(5, 5) > self._rrf(1)

    def test_earlier_rank_higher(self):
        # 同为双路，排名越靠前分越高
        assert self._rrf(1, 1) > self._rrf(5, 5) > self._rrf(50, 50)

    def test_k_value(self):
        # k=60 业界通用值（防误改）
        sys.path.insert(0, str(project_root / "skills/ask-question/script"))
        from agent import RRF_K
        assert RRF_K == 60


class TestRerankLayer:
    """rerank 层：降级语义与精排排序"""

    def test_rerank_skips_without_model(self):
        # reranker 不可用 → 返回 RRF 粗排前 k 条（优雅降级，不抛异常）
        sys.path.insert(0, str(project_root / "skills/ask-question/script"))
        import agent as rag_mod
        from agent import RAGKnowledgeAgent
        rag = RAGKnowledgeAgent.__new__(RAGKnowledgeAgent)
        rag._reranker = None
        # 强制加载失败路径：临时改路径
        orig = rag_mod.RERANKER_MODEL_PATH
        rag_mod.RERANKER_MODEL_PATH = "data/models/__no_such_model__"
        try:
            docs = [{"content": f"doc{i}", "score": 1.0 - i * 0.1} for i in range(5)]
            out = rag._rerank("q", docs, 3)
            assert len(out) == 3 and out[0]["content"] == "doc0"  # RRF 顺序原样
        finally:
            rag_mod.RERANKER_MODEL_PATH = orig

    def test_rerank_reorders_by_score(self):
        # reranker 打分后按分数重排（mock CrossEncoder：doc2 最高分应排第一）
        sys.path.insert(0, str(project_root / "skills/ask-question/script"))
        from agent import RAGKnowledgeAgent

        class _FakeCE:
            def predict(self, pairs):
                # pairs: [(q, doc.content)]，给 content=="best" 的打最高分
                return [10.0 if p[1] == "best" else 1.0 for p in pairs]

        rag = RAGKnowledgeAgent.__new__(RAGKnowledgeAgent)
        rag._reranker = _FakeCE()
        docs = [{"content": "a", "score": 0.9}, {"content": "best", "score": 0.1},
                {"content": "c", "score": 0.8}]
        out = rag._rerank("q", docs, 2)
        assert out[0]["content"] == "best"       # 精排推翻粗排
        assert out[0]["rerank_score"] == 10.0
        assert len(out) == 2


class TestM4SupplementalRetrieval:
    def _rag(self):
        sys.path.insert(0, str(project_root / "skills/ask-question/script"))
        from agent import RAGKnowledgeAgent
        rag = RAGKnowledgeAgent.__new__(RAGKnowledgeAgent)  # 跳过 __init__ 的 Milvus 连接
        from agentscope.agent import AgentBase
        AgentBase.__init__(rag)  # 但要跑基类初始化（注册 hooks，否则 reply 被元类拦截）
        rag.name = "RAGKnowledgeAgent"
        rag.model = None
        rag.initialized = True
        rag.skill_loader = None
        return rag

    def test_insufficient_detection(self):
        rag = self._rag()
        assert rag._insufficient("抱歉，知识库中没有找到相关信息。")
        assert rag._insufficient("知识库中未提及该标准")
        assert not rag._insufficient("北京住宿标准为 500 元/天。")
        assert rag._insufficient("")

    def test_no_knowledge_flow_without_model(self):
        """零命中 + 无 LLM（不补检索）→ no_knowledge"""
        rag = self._rag()
        rag.search_knowledge = lambda q, top_k=None: []
        from agentscope.message import Msg
        resp = asyncio.run(rag.reply(Msg(name="user", content="任意问题", role="user")))
        data = json.loads(resp.content)
        assert data["status"] == "no_knowledge"

    def test_hit_flow_without_model(self):
        """有命中 + 无 LLM → 返回知识原文（原行为不变）"""
        rag = self._rag()
        rag.search_knowledge = lambda q, top_k=None: [
            {"content": "北京住宿标准 500 元/天", "metadata": {"source": "policy"}}]
        from agentscope.message import Msg
        resp = asyncio.run(rag.reply(Msg(name="user", content="北京住宿标准", role="user")))
        data = json.loads(resp.content)
        assert data["status"] == "success"
        assert "500" in data["answer"]

    def test_supplemental_merges_docs(self):
        """补检索命中新文档时合并进 retrieved_documents"""
        rag = self._rag()
        rag.model = object()  # 哨兵：补检索入口的 model 守卫；实际 LLM 调用已被 mock

        calls = {"n": 0}

        def fake_search(q, top_k=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return [{"content": "片段A", "metadata": {}}]
            return [{"content": "片段A", "metadata": {}},   # 重复
                    {"content": "片段B_new", "metadata": {}}]  # 新

        rag.search_knowledge = fake_search

        async def fake_rewrite(uq):
            return "改写后的查询"

        rag._rewrite_query = fake_rewrite
        # 第一轮答案自报不足 → 触发补检索
        rag._generate_answer = None  # 确认下面 mock 生效

        gen_calls = {"n": 0}

        async def fake_gen(q, docs):
            gen_calls["n"] += 1
            if gen_calls["n"] == 1:
                return "抱歉，知识库中没有找到相关信息。"  # 首轮自报不足 → 触发补检索
            return f"基于{len(docs)}个片段的回答"

        rag._generate_answer = fake_gen

        from agentscope.message import Msg
        resp = asyncio.run(rag.reply(Msg(name="user", content="问题", role="user")))
        data = json.loads(resp.content)
        assert data["status"] == "success"
        assert data["supplemental_retrieval"]["added_documents"] == 1
        assert len(data["retrieved_documents"]) == 2
        assert calls["n"] == 2  # 补检索只多一次（有界）


# ---------------------------------------------------------------- M5

class TestM5BoundaryGovernance:
    def test_prompt_too_long_rejected(self):
        from utils.skill_runners import LLMSkillAgent, MAX_PROMPT_CHARS

        class _Loader:
            def load_skills(self):
                return {}

            def get_skill_content(self, name):
                return "任务：{{user_query}}"  # 占位符让超长 query 真正进 prompt

        agent = LLMSkillAgent(name="t", skill_name="preference", skill_loader=_Loader())
        big = "超长" * (MAX_PROMPT_CHARS // 2 + 10)
        from agentscope.message import Msg
        msg = Msg(name="Orchestrator",
                  content=json.dumps({"context": {"rewritten_query": big}}),
                  role="user")
        resp = asyncio.run(agent.reply(msg))
        data = json.loads(resp.content)
        assert data["status"] == "error"
        assert "超长" in data["message"]
        # 没烧 LLM：model 为 None，若走到 LLM 调用会抛错而非返回结构化 error

    def test_timeout_clamped(self):
        from utils.skill_runners import _skill_timeout_sec
        v = _skill_timeout_sec()
        assert 30.0 <= v <= 600.0

    def test_request_size_cap_on_chat_model(self):
        """server 层请求上限（防呆：Field max_length 存在）"""
        import inspect
        import server
        sig = inspect.signature(server.ChatRequest.model_fields if False else None) if False else None
        fields = server.ChatRequest.model_fields
        assert fields["message"].metadata, "message 缺少 max_length 约束"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

import asyncio
import json
import pytest

from utils.pi_durable_client import (
    PiDurableClient, DurableTurnTimeout, PiDurableUnavailable,
)

CFG = {
    "base_url": "http://pi-durable-test",
    "token": "t0ken",
    "submit_timeout_sec": 2.0,
    "wait_timeout_sec": 0.3,   # 测试里预算压短
    "poll_interval_sec": 0.1,
    "health_ttl_sec": 0.0,
}


def _client(handler) -> PiDurableClient:
    c = PiDurableClient(CFG)
    import httpx
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=2.0)
    return c


class TestPiDurableClient:
    def test_submit_connection_error_returns_none(self):
        def handler(request):
            raise httpx.ConnectError("boom")
        import httpx
        c = _client(handler)
        assert asyncio.run(c.submit_turn({"x": 1})) is None

    def test_submit_5xx_returns_none(self):
        def handler(request):
            return httpx.Response(503, json={"code": 1, "message": "down"})
        c = _client(handler)
        assert asyncio.run(c.submit_turn({"x": 1})) is None

    def test_submit_rejected_code_returns_none(self):
        def handler(request):
            return httpx.Response(200, json={"code": 1, "message": "bad"})
        c = _client(handler)
        assert asyncio.run(c.submit_turn({"x": 1})) is None

    def test_submit_ok(self):
        def handler(request):
            assert request.headers["x-internal-token"] == "t0ken"
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "7", "duplicate": False}})
        c = _client(handler)
        assert asyncio.run(c.submit_turn({"x": 1})) == {"task_id": "7", "duplicate": False}

    def test_wait_completed(self):
        def handler(request):
            return httpx.Response(200, json={"code": 0, "data": {
                "task_id": "7", "status": "completed", "result": {"results": [1, 2]}}})
        c = _client(handler)
        out = asyncio.run(c.wait_turn("7"))
        assert out == {"results": [1, 2]}

    def test_wait_faulted_raises_unavailable(self):
        def handler(request):
            return httpx.Response(200, json={"code": 0, "data": {
                "task_id": "7", "status": "faulted", "error": "x"}})
        c = _client(handler)
        with pytest.raises(PiDurableUnavailable):
            asyncio.run(c.wait_turn("7"))

    def test_wait_timeout_running_raises_turn_timeout(self):
        def handler(request):
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "7", "status": "running"}})
        c = _client(handler)
        with pytest.raises(DurableTurnTimeout):
            asyncio.run(c.wait_turn("7"))

    def test_available_cached_failure(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={"status": "ok"})
        c = _client(handler)
        assert asyncio.run(c.available())
        assert asyncio.run(c.available())
        assert calls["n"] == 2  # health_ttl=0 → 不缓存


class FakeOrchestrator:
    """最小 orchestrator 桩：只实现 durable_turn 用到的方法"""

    def __init__(self, gate=None):
        self._gate = gate

    def plan_batches(self, intention_data):
        if self._gate:
            return {"kind": "gate", "gate": self._gate}
        return {
            "kind": "batches",
            "batches": [
                [{"agent_name": "rag_knowledge", "priority": 1, "reason": "r", "expected_output": "e"}],
                [{"agent_name": "expense_estimate", "priority": 2, "reason": "r2", "expected_output": "e2"}],
            ],
            "reused": [{"agent_name": "budget_check", "priority": 1, "result": {"status": "success"}}],
            "fast_path": False,
            "context": {"rewritten_query": "q"},
        }

    def _get_replay(self, name):
        return "unsafe" if name == "preference" else "safe"


class TestDurableTurn:
    def test_unavailable_returns_none(self):
        from utils.durable_turn import run_durable_turn

        def handler(request):
            raise httpx.ConnectError("down")
        c = _client(handler)
        out = asyncio.run(run_durable_turn(c, FakeOrchestrator(), {"agent_schedule": [{}]},
                                           "u1", "s1", "r1", "t1"))
        assert out is None  # 降级信号

    def test_submit_none_returns_none(self):
        from utils.durable_turn import run_durable_turn

        def handler(request):
            if "/health" in str(request.url):
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(503, json={})
        c = _client(handler)
        out = asyncio.run(run_durable_turn(c, FakeOrchestrator(), {"agent_schedule": [{}]},
                                           "u1", "s1", "r1", "t1"))
        assert out is None

    def test_gate_raises_gate_result(self):
        from utils.durable_turn import run_durable_turn, DurableGateResult

        gate = {"status": "needs_input", "missing_info": ["start_date"]}
        orch = FakeOrchestrator(gate=gate)
        with pytest.raises(DurableGateResult) as exc_info:
            asyncio.run(run_durable_turn(_client(lambda r: None), orch,
                                         {"agent_schedule": [{}]}, "u1", "s1", "r1", "t1"))
        # gate 不发任何 HTTP——直接抛，且携带 needs_input 结构
        assert exc_info.value.gate["status"] == "needs_input"

    def test_payload_shape(self):
        from utils.durable_turn import run_durable_turn
        captured = {}

        def handler(request):
            if "/health" in str(request.url):
                return httpx.Response(200, json={"status": "ok"})
            if "/submit" in str(request.url):
                captured["submit"] = json.loads(request.read())
                return httpx.Response(200, json={"code": 0, "data": {"task_id": "9", "duplicate": False}})
            return httpx.Response(200, json={"code": 0, "data": {
                "task_id": "9", "status": "completed", "result": {"results": [{"agent_name": "rag_knowledge", "priority": 1, "result": {"status": "success"}}]}}})

        c = _client(handler)
        out = asyncio.run(run_durable_turn(c, FakeOrchestrator(), {"agent_schedule": [{}]},
                                           "u1", "s1", "r1", "t1"))
        p = captured["submit"]
        assert p["request_id"] == "r1" and p["user_id"] == "u1"
        assert len(p["batches"]) == 2
        assert p["batches"][0][0]["agent_name"] == "rag_knowledge"
        assert p["previous_results"][0]["agent_name"] == "budget_check"
        assert p["skill_meta"] == {"rag_knowledge": {"replay": "safe"},
                                   "expense_estimate": {"replay": "safe"}}
        assert out["results"][0]["agent_name"] == "rag_knowledge"
        assert out["fast_path"] is False


import httpx  # noqa: E402  (集中放底部避免顶部覆盖测试内的局部导入)


class TestIntentClient:
    """M7：submit_intent / wait_intent 的降级语义（意图识别幂等——一切故障都降级，无 PiDurableUnavailable）"""

    def test_submit_intent_connection_error_returns_none(self):
        def handler(request):
            raise httpx.ConnectError("down")
        c = _client(handler)
        assert asyncio.run(c.submit_intent("p", "r1", "u1")) is None

    def test_submit_intent_ok(self):
        def handler(request):
            assert request.url.path == "/v1/intent/submit"
            return httpx.Response(200, json={"code": 0, "data": {"submission_id": "s9"}})
        c = _client(handler)
        assert asyncio.run(c.submit_intent("p", "r1", "u1")) == "s9"

    def test_wait_intent_completed_returns_text(self):
        def handler(request):
            return httpx.Response(200, json={"code": 0, "data": {
                "submission_id": "s9", "status": "completed", "text": '{"intents": []}'}})
        c = _client(handler)
        assert asyncio.run(c.wait_intent("s9")) == '{"intents": []}'

    def test_wait_intent_running_timeout_returns_none(self):
        def handler(request):
            return httpx.Response(200, json={"code": 0, "data": {
                "submission_id": "s9", "status": "running"}})
        c = _client(handler)
        assert asyncio.run(c.wait_intent("s9")) is None  # 超预算 → 降级（None）

    def test_wait_intent_5xx_returns_none(self):
        def handler(request):
            return httpx.Response(500, json={})
        c = _client(handler)
        assert asyncio.run(c.wait_intent("s9")) is None


class TestSkillExecuteEndpoint:
    """POST /internal/skills/{name}/execute 的鉴权与协议（不起 LLM——用假 pipeline）"""

    def _app(self):
        from fastapi.testclient import TestClient
        import server as server_mod

        # 替身：绕过 lifespan 与真实组件
        server_mod.state.pipelines = {"default_user": _FakePipeline()}
        server_mod.state.pi_client = None
        return TestClient(server_mod.app), server_mod

    def test_no_token_header_401(self):
        # 默认令牌为 shanglv-local（内置）：无 header = 令牌不匹配 → 401
        client, _ = self._app()
        r = client.post("/internal/skills/rag_knowledge/execute", json={"user_id": "default_user"})
        assert r.status_code == 401

    def test_default_token_200(self):
        # 内置默认令牌直接可用（零配置启动的路径）
        client, _ = self._app()
        r = client.post("/internal/skills/rag_knowledge/execute",
                        headers={"x-internal-token": "shanglv-local"},
                        json={"user_id": "default_user", "session_id": "s1", "turn_id": "t1"})
        assert r.status_code == 200
        assert r.json()["code"] == 0

    def test_bad_token_401(self):
        client, _ = self._app()
        import config
        saved = config.PI_DURABLE_CONFIG.get("skill_execute_token")
        import os
        os.environ.setdefault("SKILL_EXECUTE_TOKEN", "sec")
        config.PI_DURABLE_CONFIG["skill_execute_token"] = "sec"
        try:
            r = client.post("/internal/skills/rag_knowledge/execute",
                            headers={"x-internal-token": "wrong"}, json={"user_id": "default_user"})
            assert r.status_code == 401
        finally:
            if saved is None:
                config.PI_DURABLE_CONFIG["skill_execute_token"] = ""
            else:
                config.PI_DURABLE_CONFIG["skill_execute_token"] = saved

    def test_happy_path_envelope(self):
        client, server_mod = self._app()
        import config, os
        saved = config.PI_DURABLE_CONFIG.get("skill_execute_token")
        config.PI_DURABLE_CONFIG["skill_execute_token"] = "sec"
        try:
            r = client.post("/internal/skills/rag_knowledge/execute",
                            headers={"x-internal-token": "sec"},
                            json={"user_id": "default_user", "session_id": "s1", "turn_id": "t1",
                                  "agent_name": "rag_knowledge", "priority": 1,
                                  "context": {}, "previous_results": []})
            assert r.status_code == 200
            body = r.json()
            assert body["code"] == 0
            assert body["data"]["result"]["status"] == "success"
        finally:
            config.PI_DURABLE_CONFIG["skill_execute_token"] = saved or ""


class _FakeOrchestrator:
    _skill_meta = {}

    def __init__(self):
        self._trace_ctx = {}
        self._fast_path_used = False

    def set_trace_context(self, *a, **kw):
        pass

    async def _execute_agent(self, agent_name, context, reason, expected_output,
                             previous_results, task_priority=1):
        return {"status": "success", "agent_name": agent_name, "data": {"echo": True}}


class _FakePipeline:
    def __init__(self):
        self.orchestrator = _FakeOrchestrator()

    def switch_session(self, sid):
        pass
