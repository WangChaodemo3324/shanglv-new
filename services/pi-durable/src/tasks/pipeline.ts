/**
 * 任务定义：SkillTask（单个 skill 的 HTTP 回调执行）+ PipelineTask（批次 DAG 编排）。
 *
 * 关键 durable 语义（经 smoke 脚本实测验证）：
 *  - waiting 必须与 tx.createTask 同一次 commit 返回（否则「no durable progress」fault）
 *  - checkpoint 只放业务字段；运行期业务字段在 task.state.checkpoint 下访问
 *  - SkillTask 先 commit dispatched checkpoint 再发 HTTP——崩溃后 replay:safe 重调（只读幂等），
 *    replay:unsafe（preference）直接 terminal error 跳过（其副作用在 Python turn 尾部，
 *    由 request_id 去重保证单次）
 */
import { defineTask, type TaskId } from "@earendil-works/pi-durable";
import { SkillGenTask } from "./skill_gen.js";
import { PythonClient, type SkillCallbackInput } from "../python_client.js";

// Python 回调客户端注入：durable 的 phase handler 由 runtime 调用，没有自定义参数通道，
// 用模块级单例在服务装配时注入（index.ts 启动时 setPythonClient）。
let _py: PythonClient | undefined;

export function setPythonClient(client: PythonClient | undefined): void {
  _py = client;
}

// ---------- SkillTask：通用单个 skill 执行 ----------

export interface SkillTaskInput {
  request_id: string;
  user_id: string;
  session_id: string;
  turn_id: string;
  agent_name: string;
  priority: number;
  reason: string;
  expected_output: string;
  context: Record<string, unknown>;
  /** 批次快照：到本批为止的全部前序结果（replan 复用 + 已完成批次）；consumes 过滤由 Python 端点做 */
  previous_results: Array<{ agent_name: string; priority: number; result: unknown }>;
  fast_path: boolean;
  replay: "safe" | "unsafe";
}

export interface SkillResult {
  status: string;
  [key: string]: unknown;
}

type SkillState = { phase: "call"; step: "new" | "dispatched" };

export const SkillTask = defineTask<SkillTaskInput, SkillState, SkillResult>({
  name: "shanglv.skill",
  version: 1,
  initial: () => ({ phase: "call", step: "new" }),
  phases: {
    call: async (task, runtime, ctx) => {
      const cp = task.state.checkpoint;
      // 崩溃恢复判定：已 dispatched 且 unsafe → 不重调（at-most-once）
      if (cp.step === "dispatched" && task.input.replay === "unsafe") {
        await runtime.commit(() => ({
          status: "terminal" as const,
          outcome: {
            status: "completed" as const,
            result: {
              status: "error",
              agent_name: task.input.agent_name,
              data: { error: "interrupted after dispatch (replay: unsafe)" },
              message: "进程崩溃，该 skill 不可安全重放，已跳过",
            },
          },
        }), ctx);
        return;
      }
      // 分割点：先落 dispatched 再发 HTTP（replay 语义的依据）
      await runtime.commit(() => ({
        status: "running" as const,
        checkpoint: { phase: "call" as const, step: "dispatched" as const },
      }), ctx);

      const input: SkillCallbackInput = {
        request_id: task.input.request_id,
        user_id: task.input.user_id,
        session_id: task.input.session_id,
        turn_id: task.input.turn_id,
        agent_name: task.input.agent_name,
        priority: task.input.priority,
        reason: task.input.reason,
        expected_output: task.input.expected_output,
        context: task.input.context,
        previous_results: task.input.previous_results,
        fast_path: task.input.fast_path,
      };
      const result = _py
        ? await _py.callSkill(input, runtime.signal)
        : { status: "error", agent_name: task.input.agent_name, data: { error: "python client not wired" } };

      await runtime.commit(() => ({
        status: "terminal" as const,
        outcome: { status: "completed" as const, result: result as SkillResult },
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

// ---------- PipelineTask：批次循环编排 ----------

export interface ScheduleEntry {
  agent_name: string;
  priority: number;
  reason: string;
  expected_output: string;
}

export interface PipelineTaskInput {
  request_id: string;
  user_id: string;
  session_id: string;
  turn_id: string;
  /** Python 已按相邻 priority 分好批（batches[0]=P1，batches[1]=P2…） */
  batches: ScheduleEntry[][];
  context: Record<string, unknown>;
  /** replan 复用的 P1 结果（Python 端 _apply_replan 已剔除对应调度项），排最前 */
  previous_results: Array<{ agent_name: string; priority: number; result: unknown }>;
  fast_path: boolean;
  skill_meta: Record<string, { replay: "safe" | "unsafe" }>;
}

export interface PipelineResult {
  results: Array<{ agent_name: string; priority: number; result: unknown }>;
}

type PipelineState =
  | { phase: "dispatch"; batchIndex: number; batchTaskIds: TaskId[]; results: Array<{ agent_name: string; priority: number; result: unknown }> }
  | { phase: "collect"; batchIndex: number; batchTaskIds: TaskId[]; results: Array<{ agent_name: string; priority: number; result: unknown }> };

export const PipelineTask = defineTask<PipelineTaskInput, PipelineState, PipelineResult>({
  name: "shanglv.pipeline",
  version: 1,
  initial: () => ({ phase: "dispatch", batchIndex: 0, batchTaskIds: [], results: [] }),
  phases: {
    // 为当前批次创建 SkillTask 子任务并进入持久化 waiting（allSettled = asyncio.gather(return_exceptions=True) 的 durable 等价物）
    dispatch: async (task, runtime, ctx) => {
      const cp = task.state.checkpoint;
      const batch = task.input.batches[cp.batchIndex] ?? [];
      await runtime.commit(async (tx) => {
        const ids: TaskId[] = [];
        for (const entry of batch) {
          const id = await tx.createTask(SkillGenTask, {
            request_id: task.input.request_id,
            user_id: task.input.user_id,
            session_id: task.input.session_id,
            turn_id: task.input.turn_id,
            agent_name: entry.agent_name,
            priority: entry.priority,
            reason: entry.reason ?? "",
            expected_output: entry.expected_output ?? "",
            context: task.input.context,
            // 批次快照：全量 previous_results（consumes 过滤由 Python 端点做——pi-durable 不懂 SKILL.md）
            previous_results: [...task.input.previous_results, ...cp.results],
            fast_path: task.input.fast_path,
            replay: task.input.skill_meta?.[entry.agent_name]?.replay ?? "safe",
          }, { ownership: { kind: "task", taskId: task.id } });
          ids.push(id);
        }
        return {
          status: "waiting" as const,
          checkpoint: {
            phase: "collect" as const,
            batchIndex: cp.batchIndex,
            batchTaskIds: ids,
            results: cp.results,
          },
          on: ids,
          policy: "allSettled" as const,
        };
      }, ctx);
    },
    // 子任务全 terminal 后恢复：收集 outcomes，进入下一批或终止
    collect: async (task, runtime, ctx) => {
      const cp = task.state.checkpoint;
      const outcomes = await runtime.outcomes(cp.batchTaskIds, ctx);
      const batch = task.input.batches[cp.batchIndex] ?? [];
      const results = [...cp.results];
      outcomes.forEach((outcome, i) => {
        const entry = batch[i];
        if (!entry) return;
        results.push({
          agent_name: entry.agent_name,
          priority: entry.priority,
          result: outcome.status === "completed"
            ? (outcome as { result?: unknown }).result
            : { status: "error", agent_name: entry.agent_name, data: { error: String((outcome as { error?: unknown }).error ?? outcome.status) } },
        });
      });
      const nextIndex = cp.batchIndex + 1;
      if (nextIndex < task.input.batches.length) {
        // running + 新 checkpoint → 调度器再次拾起，进入下一批 dispatch
        await runtime.commit(() => ({
          status: "running" as const,
          checkpoint: {
            phase: "dispatch" as const,
            batchIndex: nextIndex,
            batchTaskIds: [],
            results,
          },
        }), ctx);
        return;
      }
      await runtime.commit(() => ({
        status: "terminal" as const,
        outcome: {
          status: "completed" as const,
          result: { results: [...task.input.previous_results, ...results] },
        },
      }), ctx);
    },
  },
  abort: async (_task, runtime, ctx) => {
    // ownership 级联会自动 abort 全部 SkillTask 子任务；这里只写终止标记（本期无用户侧 abort 入口）
    await runtime.commit(() => ({
      status: "terminal" as const,
      outcome: {
        status: "aborted" as const,
        reason: "pipeline aborted",
        result: { results: [] },
      },
    }), ctx);
  },
});
