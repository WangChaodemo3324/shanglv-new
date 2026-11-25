/**
 * SkillGenTask（M8）：skill 执行 + 生成统一在 pi-durable 上。
 *
 * stage 循环状态机（「概率进 pi，确定留 Python」）：
 *   phase prepare —— HTTP 回调 Python /internal/skills/{name}/prepare（毫秒级，确定性：
 *                    拼 prompt / 记忆取数 / consumes 过滤 / 边界检查）
 *       返回 {action:"generate", prompt} —— 要跑 LLM（进 generate）
 *            {action:"result", result}   —— 短路终态（超长拒绝 / 未注册 / 降级形态）
 *   phase generate —— 短命 conversation 上 submit GenerationTask（LLM 调用发生地，
 *                    模型轮次级 checkpoint），等 assistant 文本
 *   phase parse —— HTTP 回调 Python /internal/skills/{name}/parse（毫秒级，确定性：
 *                  四层 JSON 解析 + 契约归一化 + 确定性校验）
 *       返回 {action:"result", result} —— 终态
 *            {action:"retry", prompt}   —— 校验打回重生成（新 stage 回 generate）
 *
 * 崩溃语义：conversationId/requestId 先落 checkpoint 再 submit——重放该 phase 时
 * conversation 级 requestId 去重返回既有 submission，LLM 不双跑；answer 文本不落
 * checkpoint（从 conversation transcript 重读，持久）。
 */
import { defineTask, type Conversation } from "@earendil-works/pi-durable";
import { context } from "../harness.js";
import { SkillConversations } from "../skill_conversations.js";
import type { PythonClient, SkillCallbackInput } from "../python_client.js";

export interface SkillGenInput {
  request_id: string;
  user_id: string;
  session_id: string;
  turn_id: string;
  agent_name: string;
  priority: number;
  reason: string;
  expected_output: string;
  context: Record<string, unknown>;
  /** 批次快照：到本批为止的全部前序结果（consumes 过滤由 Python prepare 端点做） */
  previous_results: Array<{ agent_name: string; priority: number; result: unknown }>;
  fast_path: boolean;
  replay: "safe" | "unsafe";
}

export interface SkillGenResult {
  status: string;
  [key: string]: unknown;
}

type SkillGenState =
  | { phase: "prepare"; stage: number }
  | { phase: "generate"; stage: number; conversationId: string; requestId: string }
  | { phase: "parse"; stage: number; conversationId: string; requestId: string };

// 注入点（index.ts 装配）：任务 phase 里没有自定义参数通道，用模块级单例
let _skills: SkillConversations | undefined;
let _py: PythonClient | undefined;

export function setSkillGenDeps(
  skills: SkillConversations | undefined,
  py: PythonClient | undefined,
): void {
  _skills = skills;
  _py = py;
}

function genRequestId(input: SkillGenInput, stage: number): string {
  return `gen:${input.turn_id}:${input.agent_name}:${stage}`;
}

function callbackInput(task: { input: SkillGenInput }): SkillCallbackInput {
  const i = task.input;
  return {
    request_id: i.request_id,
    user_id: i.user_id,
    session_id: i.session_id,
    turn_id: i.turn_id,
    agent_name: i.agent_name,
    priority: i.priority,
    reason: i.reason,
    expected_output: i.expected_output,
    context: i.context,
    previous_results: i.previous_results,
    fast_path: i.fast_path,
  };
}

export const SkillGenTask = defineTask<SkillGenInput, SkillGenState, SkillGenResult>({
  name: "shanglv.skill.gen",
  version: 1,
  initial: () => ({ phase: "prepare", stage: 0 }),
  phases: {
    prepare: async (task, runtime, ctx) => {
      if (!_py) throw new Error("python client not wired");
      const prepare = await _py.callSkillPrepare(callbackInput(task));
      if (prepare.action === "result") {
        // P0 兼容：agent.py 型 skill（ask-question/plan-trip/query-info）的 prepare 返回
        // 「不支持 prepare 协议」降级 error —— 回落到旧整段 execute（Python 内含 LLM，M8 P1/P2 再迁）
        const r = prepare.result as { data?: { error?: string }; message?: string };
        if (r?.data?.error?.includes("P0 范围外") || r?.message?.includes("P0 范围外")) {
          const legacy = await _py.callSkill(callbackInput(task));
          await runtime.commit(() => ({
            status: "terminal" as const,
            outcome: { status: "completed" as const, result: legacy as SkillGenResult },
          }), ctx);
          return;
        }
        await runtime.commit(() => ({
          status: "terminal" as const,
          outcome: { status: "completed" as const, result: prepare.result as SkillGenResult },
        }), ctx);
        return;
      }
      // 进 generate：requestId 先落 checkpoint（幂等键）
      await runtime.commit(() => ({
        status: "running" as const,
        checkpoint: {
          phase: "generate" as const,
          stage: task.state.checkpoint.stage,
          conversationId: "",
          requestId: genRequestId(task.input, task.state.checkpoint.stage),
        },
      }), ctx);
    },

    generate: async (task, runtime, ctx) => {
      if (!_py || !_skills) throw new Error("deps not wired");
      const cp = task.state.checkpoint;
      // conversation 未建则建；conversationId 先落 checkpoint 再 submit（幂等纪律）
      let conversationId = cp.conversationId;
      if (!conversationId) {
        const conversation = await _skills.create();
        conversationId = String(conversation.id);
        await runtime.commit(() => ({
          status: "running" as const,
          checkpoint: { phase: "generate" as const, stage: cp.stage, conversationId, requestId: cp.requestId },
        }), ctx);
      }
      if (!_skills) throw new Error("skills not wired");
      const conversation = await _skills.getConversation(conversationId);
      if (!conversation) throw new Error(`conversation ${conversationId} not found`);

      // prompt 经 prepare 重拿（确定性拼装：同输入同输出，重放安全）
      const prepare = await _py.callSkillPrepare(callbackInput(task));
      if (prepare.action !== "generate") {
        throw new Error("prepare replay returned non-generate");
      }

      // submit（requestId 幂等：重放返回既有 submission）并等完成
      const submission = await conversation.submit(
        { type: "input", content: prepare.prompt, requestId: cp.requestId ?? genRequestId(task.input, cp.stage) },
        ctx,
      );
      const settled = await submission.wait(ctx) as { status: string; answer?: string };
      if (settled.status !== "done" || !settled.answer) {
        await runtime.commit(() => ({
          status: "terminal" as const,
          outcome: {
            status: "completed" as const,
            result: {
              status: "error",
              agent_name: task.input.agent_name,
              data: { error: `generation unanswered: ${settled.status}` },
              message: "生成未完成",
            } satisfies SkillGenResult,
          },
        }), ctx);
        return;
      }
      // answer 文本不落 checkpoint（parse 时从 transcript 重读）
      await runtime.commit(() => ({
        status: "running" as const,
        checkpoint: { phase: "parse" as const, stage: cp.stage, conversationId, requestId: cp.requestId },
      }), ctx);
    },

    parse: async (task, runtime, ctx) => {
      if (!_py || !_skills) throw new Error("deps not wired");
      const cp = task.state.checkpoint;
      if (!_skills) throw new Error("skills not wired");
      const conversation = await _skills.getConversation(cp.conversationId);
      if (!conversation) throw new Error(`conversation ${cp.conversationId} not found`);
      // 从 transcript 读最后的 assistant 文本（重放安全：transcript 持久）
      const page = await conversation.entries({ order: "descending" } as never, 5, undefined, ctx);
      const lastAssistant = (page.items ?? []).find((e) =>
        (e as { model?: Array<{ role?: string }> }).model?.[0]?.role === "assistant");
      if (!lastAssistant) throw new Error("answer entry not found in conversation");
      const text = await _skills.readAnswerText(conversation, String((lastAssistant as { id: unknown }).id));
      if (text === null) throw new Error("answer text not found");

      const parsed = await _py.callSkillParse({
        request_id: task.input.request_id,
        user_id: task.input.user_id,
        session_id: task.input.session_id,
        turn_id: task.input.turn_id,
        agent_name: task.input.agent_name,
      }, text);
      if (parsed.action === "result") {
        await runtime.commit(() => ({
          status: "terminal" as const,
          outcome: { status: "completed" as const, result: parsed.result as SkillGenResult },
        }), ctx);
        return;
      }
      // retry（校验打回）：新 stage 新 conversation，回 generate
      const nextStage = cp.stage + 1;
      await runtime.commit(() => ({
        status: "running" as const,
        checkpoint: {
          phase: "generate" as const,
          stage: nextStage,
          conversationId: "",
          requestId: genRequestId(task.input, nextStage),
        },
      }), ctx);
    },
  },
  abort: async (task, runtime, ctx) => {
    await runtime.commit(() => ({
      status: "terminal" as const,
      outcome: {
        status: "aborted" as const,
        reason: `skill ${task.input.agent_name} aborted`,
      },
    }), ctx);
  },
});
