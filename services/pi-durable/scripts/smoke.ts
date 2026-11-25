/**
 * M6 API smoke：不开 HTTP，直接验证 durable 任务链的关键 API 假设。
 *   1) Harness.open + root conversation + resume
 *   2) conversation.commit 内 tx.createTask + RequestIndex 文档原子去重
 *   3) PipelineTask 状态机：dispatch(waiting allSettled) → collect → terminal
 *   4) waitForTask / outcomes / taskGraph
 *   5) close 后重开（running→pending 恢复语义的简化观察）
 *
 * 运行：cd services/pi-durable && npm run smoke
 */
import { Harness, MemoryStorage, defineTask, defineDoc, createRegistry, defineExtension, ROOT_CONVERSATION_ID } from "@earendil-works/pi-durable";
import { createModels } from "@earendil-works/pi-ai";
import { TODO_CONTEXT, type Context } from "@earendil-works/chord/context";

// ---------- 假 Python 回调（真实实现里是 HTTP 打回 Python） ----------
let callCount = 0;
async function fakePythonSkill(agentName: string): Promise<{ status: string; agent_name: string; data: unknown }> {
  callCount += 1;
  console.log(`  [callback] #${callCount} ${agentName}`);
  return { status: "success", agent_name: agentName, data: { echo: agentName, n: callCount } };
}

// ---------- SkillTask：先 commit dispatched 再发回调 ----------
export const SkillTask = defineTask({
  name: "shanglv.skill",
  version: 1,
  input: null as unknown as { agentName: string },
  initial: () => ({ phase: "call", step: "new" as "new" | "dispatched" }),
  phases: {
    call: async (task, runtime, ctx) => {
      if (task.state.step === "dispatched" && task.input.replay === "unsafe") {
        await runtime.commit(() => ({
          status: "terminal" as const,
          outcome: { status: "completed" as const, result: { status: "error", skipped: true } },
        }), ctx);
        return;
      }
      await runtime.commit(() => ({ status: "running" as const, checkpoint: { phase: "call", step: "dispatched" } }), ctx);
      const result = await fakePythonSkill(task.input.agentName);
      await runtime.commit(() => ({
        status: "terminal" as const,
        outcome: { status: "completed" as const, result },
      }), ctx);
    },
  },
  abort: async (_task, runtime, ctx) => {
    await runtime.commit(() => ({
      status: "terminal" as const,
      outcome: { status: "aborted" as const, reason: "smoke abort" },
    }), ctx);
  },
});

// ---------- PipelineTask：批次循环 dispatch→collect ----------
type PipelineInput = {
  requestId: string;
  batches: { agentName: string; replay: "safe" | "unsafe" }[][];
};
type PipelineCheckpoint = {
  phase: "dispatch" | "collect";
  batchIndex: number;
  batchTaskIds: string[];
  results: unknown[];
};
export const PipelineTask = defineTask({
  name: "shanglv.pipeline",
  version: 1,
  input: null as unknown as PipelineInput,
  initial: () => ({ phase: "dispatch", batchIndex: 0, batchTaskIds: [], results: [] } satisfies PipelineCheckpoint),
  phases: {
    dispatch: async (task, runtime, ctx) => {
      const cp = task.state.checkpoint;
      const batch = task.input.batches[cp.batchIndex] ?? [];
      // 官方 24-child-tasks 范式：createTask 与 waiting 状态必须同一次 commit 提交。
      // 注意 checkpoint 只放业务字段（phase/batchIndex/...），不要展开整个 task.state（那会嵌套）
      await runtime.commit(async (tx) => {
        const ids: string[] = [];
        for (const entry of batch) {
          const id = await tx.createTask(SkillTask, { agentName: entry.agentName, replay: entry.replay },
            { ownership: { kind: "task", taskId: task.id } });
          ids.push(id);
        }
        return {
          status: "waiting" as const,
          checkpoint: { phase: "collect" as const, batchIndex: cp.batchIndex, batchTaskIds: ids, results: cp.results },
          on: ids,
          policy: "allSettled" as const,
        };
      }, ctx);
    },
    collect: async (task, runtime, ctx) => {
      // 运行时 state = {status, checkpoint}；业务字段在 task.state.checkpoint 下
      const cp = task.state.checkpoint;
      const outcomes = await runtime.outcomes(cp.batchTaskIds as never[], ctx);
      const results = [...cp.results, ...outcomes.map((o) => (o as { result?: unknown }).result ?? o)];
      const nextIndex = cp.batchIndex + 1;
      if (nextIndex < task.input.batches.length) {
        await runtime.commit(() => ({
          status: "running" as const,
          checkpoint: { phase: "dispatch" as const, batchIndex: nextIndex, batchTaskIds: [], results },
        }), ctx);
        return;
      }
      await runtime.commit(() => ({
        status: "terminal" as const,
        outcome: { status: "completed" as const, result: { results } },
      }), ctx);
    },
  },
  abort: async (_task, runtime, ctx) => {
    await runtime.commit(() => ({
      status: "terminal" as const,
      outcome: { status: "aborted" as const, reason: "smoke abort" },
    }), ctx);
  },
});

// ---------- RequestIndex：conversation 级 requestId → taskId 索引 ----------
export const RequestIndex = defineDoc({
  kind: "shanglv.request_index",
  version: 1,
  scope: "conversation",
  history: "latest",
  initial: () => ({ entries: {} as Record<string, { taskId: string; createdAt: number }> }),
});

// ---------- 装配 ----------
const registry = createRegistry();
registry.install(defineExtension({
  name: "shanglv.pipeline.core",
  tasks: [PipelineTask, SkillTask],
  docs: [RequestIndex],
}));
const models = createModels();
const ctx: Context = TODO_CONTEXT;

async function main() {
  const storage = new MemoryStorage();
  const harness = await Harness.open(storage, { models, registry }, ctx);
  harness.resume();
  const root = await harness.root(ctx, { agent: { model: { provider: "openai", modelId: "gpt-4o" } } });
  console.log("1. open+root ok, root id =", root.id, "(ROOT:", ROOT_CONVERSATION_ID, ")");

  // 提交两轮：两批 DAG（P1×2 → P2×1）
  const submit = async (requestId: string) => {
    return await root.commit(async (tx) => {
      const index = await tx.doc(RequestIndex, root.id);
      const existing = index.entries[requestId];
      if (existing) return { taskId: existing.taskId, duplicate: true };
      const taskId = await tx.createTask(PipelineTask, {
        requestId,
        batches: [
          [{ agentName: "rag_knowledge", replay: "safe" }, { agentName: "weather_alert", replay: "safe" }],
          [{ agentName: "expense_estimate", replay: "safe" }],
        ],
      }, { ownership: { kind: "conversation" } });
      // 修剪：只留最近 1000 条（smoke 不触发）
      index.entries[requestId] = { taskId, createdAt: Date.now() };
      return { taskId, duplicate: false };
    }, ctx);
  };

  const r1 = await submit("req-1");
  console.log("2. submit req-1 →", r1);

  const settled = await harness.waitForTask(r1.taskId as never, ctx);
  console.log("3. settled state:", JSON.stringify(settled.state).slice(0, 300));

  const r1b = await submit("req-1");
  console.log("4. duplicate submit →", r1b);
  const r2 = await submit("req-2");
  console.log("5. distinct submit →", r2);

  const graph = await harness.taskGraph(ctx);
  const g = (graph as unknown as { value?: unknown }).value ?? graph;
  console.log("6. taskGraph:", JSON.stringify(g).slice(0, 300));

  await harness.waitForTask(r2.taskId as never, ctx);
  const second = await harness.waitForTask(r2.taskId as never, ctx);
  console.log("7. second turn settled:", JSON.stringify(second.state.outcome?.result).slice(0, 150));
  console.log("8. callback total:", callCount, "(expect 6: 3 skills × 2 turns)");

  // close/reopen 观察：重开同一 storage（MemoryStorage 不持久，换 JSONL/SQLite 才真恢复——这里只验证 API 形态）
  await harness.close(ctx);
  console.log("9. closed ok");
}

main().catch((err) => { console.error("SMOKE FAILED:", err); process.exit(1); });
