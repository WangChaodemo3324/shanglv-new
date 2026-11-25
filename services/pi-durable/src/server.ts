/**
 * Fastify HTTP 层（仿 wenshu services/pi-agent/server.ts）：
 * X-Internal-Token timingSafeEqual 鉴权（未配 token 503）+ 入参校验 + 统一 {code,message,data} 信封。
 *
 * 端点：
 *   GET  /health                免鉴权探活
 *   POST /v1/pipeline/submit    提交一轮编排（RequestIndex 原子去重）
 *   POST /v1/pipeline/wait      长轮询等待（completed / running / faulted 三态）
 *   GET  /v1/pipeline/status    单任务状态（观测）
 *   GET  /v1/taskgraph          任务树快照（观测）
 */
import Fastify, { type FastifyInstance } from "fastify";
import { timingSafeEqual } from "node:crypto";
import type { Config } from "./config.js";
import type { DurableRuntime } from "./harness.js";
import { context } from "./harness.js";
import { RequestIndex, REQUEST_INDEX_LIMIT } from "./registry.js";
import { PipelineTask, type PipelineTaskInput } from "./tasks/pipeline.js";
import { IntentConversations } from "./intent.js";

const MAX_BATCHES = 8;
const MAX_ENTRIES_PER_BATCH = 24;
const MAX_PAYLOAD_BYTES = 2_000_000;

function internalAuthorized(config: Config, header: string | string[] | undefined): boolean {
  if (!config.internalToken || typeof header !== "string") return false;
  const expected = Buffer.from(config.internalToken);
  const supplied = Buffer.from(header);
  return expected.length === supplied.length && timingSafeEqual(expected, supplied);
}

interface SubmitBody {
  request_id?: string;
  user_id?: string;
  session_id?: string;
  turn_id?: string;
  batches?: unknown;
  context?: unknown;
  previous_results?: unknown;
  fast_path?: boolean;
  skill_meta?: unknown;
}

export function buildApp(config: Config, runtime: DurableRuntime): FastifyInstance {
  const app = Fastify({ logger: false });
  const { harness } = runtime;
  const intents = new IntentConversations(harness);

  app.get("/health", async () => ({
    status: "ok",
    service: "shanglv-pi-durable-service",
    storage: "sqlite",
    root: runtime.rootId,
  }));

  app.addHook("preHandler", async (req, reply) => {
    if (req.url === "/health" || req.url === "/") return;
    if (!internalAuthorized(config, req.headers["x-internal-token"])) {
      return reply.code(config.internalToken ? 401 : 503).send({
        code: 1,
        message: config.internalToken ? "内部服务令牌无效" : "内部服务令牌未配置",
      });
    }
  });

  app.post("/v1/pipeline/submit", async (req, reply) => {
    const body = (req.body ?? {}) as SubmitBody;
    if (typeof body.request_id !== "string" || !body.request_id) {
      return reply.code(400).send({ code: 1, message: "request_id 必填" });
    }
    if (typeof body.user_id !== "string" || !body.user_id) {
      return reply.code(400).send({ code: 1, message: "user_id 必填" });
    }
    if (!Array.isArray(body.batches) || body.batches.length === 0 || body.batches.length > MAX_BATCHES) {
      return reply.code(400).send({ code: 1, message: `batches 必填且 1..${MAX_BATCHES} 批` });
    }
    for (const batch of body.batches) {
      if (!Array.isArray(batch) || batch.length === 0 || batch.length > MAX_ENTRIES_PER_BATCH) {
        return reply.code(400).send({ code: 1, message: `每批需 1..${MAX_ENTRIES_PER_BATCH} 个调度项` });
      }
      for (const entry of batch) {
        const e = entry as Record<string, unknown>;
        if (typeof e.agent_name !== "string" || !e.agent_name) {
          return reply.code(400).send({ code: 1, message: "调度项缺 agent_name" });
        }
      }
    }
    const payloadSize = JSON.stringify(body).length;
    if (payloadSize > MAX_PAYLOAD_BYTES) {
      return reply.code(413).send({ code: 1, message: "payload 超出大小限制" });
    }

    const input: PipelineTaskInput = {
      request_id: body.request_id.slice(0, 128),
      user_id: body.user_id.slice(0, 64),
      session_id: (body.session_id ?? "").slice(0, 64),
      turn_id: (body.turn_id ?? "").slice(0, 64),
      batches: body.batches as PipelineTaskInput["batches"],
      context: (body.context ?? {}) as Record<string, unknown>,
      previous_results: (body.previous_results ?? []) as PipelineTaskInput["previous_results"],
      fast_path: Boolean(body.fast_path),
      skill_meta: (body.skill_meta ?? {}) as PipelineTaskInput["skill_meta"],
    };

    const root = await harness.root(context);
    const result = await root.commit(async (tx) => {
      const index = await tx.doc(RequestIndex, root.id);
      const key = `${input.user_id}:${input.request_id}`;
      const existing = index.entries[key];
      if (existing) {
        return { task_id: existing.taskId, duplicate: true };
      }
      const taskId = await tx.createTask(PipelineTask, input, { ownership: { kind: "conversation" } });
      // 修剪：超出上限淘汰最旧条目（entries 无序，按 createdAt 排序）
      const keys = Object.keys(index.entries);
      if (keys.length >= REQUEST_INDEX_LIMIT) {
        const sorted = keys.sort((a, b) => index.entries[a].createdAt - index.entries[b].createdAt);
        for (const k of sorted.slice(0, keys.length - REQUEST_INDEX_LIMIT + 1)) {
          delete index.entries[k];
        }
      }
      index.entries[key] = { taskId: String(taskId), createdAt: Date.now() };
      return { task_id: String(taskId), duplicate: false };
    }, context);

    return { code: 0, message: "ok", data: result };
  });

  app.post("/v1/pipeline/wait", async (req, reply) => {
    const body = (req.body ?? {}) as { task_id?: string; timeout_ms?: number };
    if (typeof body.task_id !== "string" || !body.task_id) {
      return reply.code(400).send({ code: 1, message: "task_id 必填" });
    }
    const timeoutMs = Math.max(1_000, Math.min(Number(body.timeout_ms ?? 30_000), config.waitMaxMs));

    type WaitOutcome =
      | { kind: "settled"; settled: Awaited<ReturnType<typeof harness.waitForTask>> }
      | { kind: "timeout" };
    const waitOutcome: WaitOutcome = await Promise.race([
      harness.waitForTask(body.task_id as never, context).then((settled): WaitOutcome => ({ kind: "settled", settled })),
      new Promise<WaitOutcome>((resolve) => {
        setTimeout(() => resolve({ kind: "timeout" }), timeoutMs);
      }),
    ]);

    if (waitOutcome.kind === "timeout") {
      return { code: 0, message: "ok", data: { task_id: body.task_id, status: "running" } };
    }
    const state = waitOutcome.settled.state as { status?: string; outcome?: { status?: string; result?: unknown; error?: unknown } };
    // 防御：waitForTask 可能对非 terminal 状态 resolve（waiting/running 中途）——一律按 running 继续轮询
    if (state.status !== "terminal" || !state.outcome) {
      return { code: 0, message: "ok", data: { task_id: body.task_id, status: "running" } };
    }
    if (state.outcome.status === "completed") {
      return {
        code: 0,
        message: "ok",
        data: { task_id: body.task_id, status: "completed", result: state.outcome.result },
      };
    }
    // failed / aborted / orphaned / faulted —— 统一按 faulted 上报（Python 侧 503 提示重试）
    const errorText = (() => {
      try {
        return JSON.stringify(state.outcome).slice(0, 500);
      } catch {
        return String(state.outcome.status);
      }
    })();
    return {
      code: 0,
      message: "ok",
      data: { task_id: body.task_id, status: "faulted", error: errorText },
    };
  });

  app.get("/v1/pipeline/status", async (req) => {
    const taskId = (req.query as { task_id?: string }).task_id;
    if (!taskId) return { code: 1, message: "task_id 必填" };
    const record = await harness.getTask(taskId as never, context);
    if (!record) return { code: 1, message: "task not found" };
    return {
      code: 0,
      message: "ok",
      data: { task_id: taskId, state: record.state },
    };
  });

  app.get("/v1/taskgraph", async () => {
    const graph = await harness.taskGraph(context);
    const value = (graph as unknown as { value?: unknown }).value;
    graph.dispose();
    return { code: 0, message: "ok", data: { graph: value } };
  });

  // ---------- M7：意图识别 GenerationTask 端点 ----------

  const MAX_PROMPT_CHARS = 60_000; // 与 Python 侧 runner 的 prompt 上限同口径

  app.post("/v1/intent/submit", async (req, reply) => {
    const body = (req.body ?? {}) as { request_id?: string; user_id?: string; prompt?: string };
    if (typeof body.request_id !== "string" || !body.request_id) {
      return reply.code(400).send({ code: 1, message: "request_id 必填" });
    }
    if (typeof body.user_id !== "string" || !body.user_id) {
      return reply.code(400).send({ code: 1, message: "user_id 必填" });
    }
    if (typeof body.prompt !== "string" || !body.prompt) {
      return reply.code(400).send({ code: 1, message: "prompt 必填" });
    }
    if (body.prompt.length > MAX_PROMPT_CHARS) {
      return reply.code(413).send({ code: 1, message: "prompt 超出大小限制" });
    }
    try {
      const data = await intents.submit(body.user_id.slice(0, 64), body.request_id.slice(0, 128), body.prompt);
      return { code: 0, message: "ok", data };
    } catch (err) {
      return reply.code(502).send({ code: 1, message: `intent submit 失败: ${err}` });
    }
  });

  app.post("/v1/intent/wait", async (req, reply) => {
    const body = (req.body ?? {}) as { submission_id?: string; timeout_ms?: number };
    if (typeof body.submission_id !== "string" || !body.submission_id) {
      return reply.code(400).send({ code: 1, message: "submission_id 必填" });
    }
    const timeoutMs = Math.max(1_000, Math.min(Number(body.timeout_ms ?? 30_000), 120_000));
    try {
      const text = await intents.wait(body.submission_id, timeoutMs);
      if (text === null) {
        return { code: 0, message: "ok", data: { submission_id: body.submission_id, status: "running" } };
      }
      return { code: 0, message: "ok", data: { submission_id: body.submission_id, status: "completed", text } };
    } catch (err) {
      return reply.code(502).send({ code: 1, message: `intent wait 失败: ${err}` });
    }
  });

  return app;
}
