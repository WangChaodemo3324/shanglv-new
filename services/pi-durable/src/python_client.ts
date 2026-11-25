/**
 * Python 回调客户端：SkillGenTask 的 prepare/parse 出口（M8 起）。
 * prepare/parse 都是毫秒级确定性逻辑（拼 prompt / 解析校验），超时秒级；
 * 旧 callSkill（整段 execute 含 LLM）保留给降级兼容。
 */
import type { Config } from "./config.js";

export interface SkillCallbackInput {
  request_id: string;
  user_id: string;
  session_id: string;
  turn_id: string;
  agent_name: string;
  priority: number;
  reason: string;
  expected_output: string;
  context: Record<string, unknown>;
  previous_results: unknown[];
  fast_path: boolean;
}

export interface PrepareResult {
  /** generate：要跑 LLM，prompt 为完整生成提示 */
  action: "generate";
  prompt: string;
  /** retry：换 prompt 重试（M8 预留：ask-question 补检索等） */
  stage_note?: string;
}

export interface ParseResult {
  /** result：终态 */
  action: "result";
  result: Record<string, unknown>;
  /** retry：校验打回，prompt 为修正后的重试提示 */
}

export type ParseOutcome =
  | { action: "result"; result: Record<string, unknown> }
  | { action: "retry"; prompt: string };

export type PrepareOutcome =
  | { action: "generate"; prompt: string }
  | { action: "result"; result: Record<string, unknown> };

export interface SkillEnvelope {
  code: number;
  message: string;
  data?: Record<string, unknown>;
}

const PREPARE_PARSE_TIMEOUT_MS = 10_000; // 毫秒级回调，10s 已很宽裕

export class PythonClient {
  constructor(private readonly config: Config) {}

  private async post(path: string, body: unknown, timeoutMs: number, signal?: AbortSignal): Promise<SkillEnvelope> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    // 外部 signal（durable abort）也要能打断回调
    signal?.addEventListener("abort", () => controller.abort(), { once: true });
    try {
      const res = await fetch(`${this.config.pyBaseUrl}${path}`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-internal-token": this.config.skillCallbackToken,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) {
        return { code: 1, message: `python ${path} -> HTTP ${res.status}` };
      }
      return (await res.json()) as SkillEnvelope;
    } finally {
      clearTimeout(timer);
    }
  }

  /** M8 prepare：Python 拼好 prompt（含检索/记忆/consumes 过滤）或返回短路终态 */
  async callSkillPrepare(input: SkillCallbackInput, signal?: AbortSignal): Promise<PrepareOutcome> {
    const out = await this.withPrepareFallback<PrepareOutcome>(input, async () => {
      const envelope = await this.post(
        `/internal/skills/${encodeURIComponent(input.agent_name)}/prepare`,
        input,
        PREPARE_PARSE_TIMEOUT_MS,
        signal,
      );
      return this.unwrapStage(envelope, input.agent_name) as unknown as PrepareOutcome;
    });
    return out;
  }

  /** M8 parse：Python 解析文本 + 归一化 + 校验（可返回 retry prompt） */
  async callSkillParse(
    input: { request_id: string; user_id: string; session_id: string; turn_id: string; agent_name: string },
    text: string,
    signal?: AbortSignal,
  ): Promise<ParseOutcome> {
    return await this.withPrepareFallback<ParseOutcome>(input, async () => {
      const envelope = await this.post(
        `/internal/skills/${encodeURIComponent(input.agent_name)}/parse`,
        { ...input, text },
        PREPARE_PARSE_TIMEOUT_MS,
        signal,
      );
      return this.unwrapStage(envelope, input.agent_name) as unknown as ParseOutcome;
    });
  }

  private unwrapStage(envelope: SkillEnvelope, agentName: string): PrepareOutcome | ParseOutcome {
    if (envelope.code === 0 && envelope.data && typeof (envelope.data as { action?: unknown }).action === "string") {
      return envelope.data as unknown as PrepareOutcome | ParseOutcome;
    }
    // Python 拒绝（token/未知 agent/内部异常）——合成同形 error 终态
    return {
      action: "result",
      result: {
        status: "error",
        agent_name: agentName,
        data: { error: envelope.message || "python callback rejected" },
        message: envelope.message || "python callback rejected",
      },
    };
  }

  /** prepare/parse 传输失败：合成 error 终态（不中断整轮，聚合标 partial_failure） */
  private async withPrepareFallback<T extends PrepareOutcome | ParseOutcome>(
    input: { agent_name: string },
    fn: () => Promise<T>,
  ): Promise<T> {
    let lastError = "unknown";
    for (let attempt = 0; attempt <= this.config.skillHttpRetries; attempt++) {
      try {
        return await fn();
      } catch (err) {
        lastError = String(err);
      }
    }
    return {
      action: "result",
      result: {
        status: "error",
        agent_name: input.agent_name,
        data: { error: `skill callback failed after retries: ${lastError}` },
        message: `智能体执行失败（回调）: ${lastError}`,
      },
    } as unknown as T;
  }

  /** 旧整段 execute 回调（本地含 LLM）——降级兼容保留 */
  async callSkill(input: SkillCallbackInput, signal?: AbortSignal): Promise<Record<string, unknown>> {
    let lastError = "unknown";
    for (let attempt = 0; attempt <= this.config.skillHttpRetries; attempt++) {
      if (signal?.aborted) throw new Error("aborted");
      try {
        const envelope = await this.post(
          `/internal/skills/${encodeURIComponent(input.agent_name)}/execute`,
          input,
          this.config.skillHttpTimeoutMs,
          signal,
        );
        if (envelope.code === 0 && envelope.data) {
          const result = (envelope.data as { result?: Record<string, unknown> }).result;
          if (result) return result;
        }
        // code:1 = Python 侧回调层失败（token/未知 agent/内部异常）——合成同形 error result
        return {
          status: "error",
          agent_name: input.agent_name,
          data: { error: envelope.message || "python callback rejected" },
          message: envelope.message || "python callback rejected",
        };
      } catch (err) {
        lastError = String(err);
        // 超时/连接错误 → 重试（下一轮 attempt）
      }
    }
    // 传输层失败耗尽重试：合成与本地 _execute_agent 异常路径同形的结果（不中断整轮，聚合标 partial_failure）
    return {
      status: "error",
      agent_name: input.agent_name,
      data: { error: `skill callback failed after retries: ${lastError}` },
      message: `智能体执行失败（回调）: ${lastError}`,
    };
  }
}
