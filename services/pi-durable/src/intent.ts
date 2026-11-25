/**
 * IntentConversations（M7）：每用户一个 durable conversation，承载意图识别的 GenerationTask run。
 *
 * 与 pipeline root conversation 分离：
 *  - GenerationTask 的 run/inbox/boundary 语义作用在 conversation 上，
 *    混在 root 会与 PipelineTask 的 RequestIndex 原子提交争用 mutation line；
 *  - intent conversation 只做「一问一答」——Python 拼好全量 prompt 提交（content），
 *    instructions 为空（prompt 单一事实源在 Python intention_agent.build_prompt）。
 *
 * requestId 防重：durable submission 的 conversation 级去重——同键重发返回既有
 * submission（含已 settled），Python 网络超时重试安全不双跑。
 */
import type { Harness, Conversation, Submission } from "@earendil-works/pi-durable";
import { context } from "./harness.js";
import { intentModelRef } from "./providers.js";

export class IntentConversations {
  private readonly cache = new Map<string, Conversation>();

  constructor(private readonly harness: Harness) {}

  /** 按用户取/建 intent conversation（模型固定为 intent 模型，无工具，无 instructions） */
  async get(userId: string): Promise<Conversation> {
    const cached = this.cache.get(userId);
    if (cached) return cached;
    const conversation = await this.harness.createConversation(
      { ownership: { kind: "ownerless" }, agent: { model: intentModelRef(), instructions: "" } },
      context,
    );
    this.cache.set(userId, conversation);
    return conversation;
  }

  /** 提交意图识别 prompt。durable 对同 requestId 返回既有 submission（幂等） */
  async submit(userId: string, requestId: string, prompt: string): Promise<{ submission_id: string }> {
    const conversation = await this.get(userId);
    const submission: Submission = await conversation.submit(
      { type: "input", content: prompt, requestId },
      context,
    );
    return { submission_id: String(submission.id) };
  }

  /** 等待意图识别完成；返回最终 assistant 文本，未完成（running）返回 null。 */
  async wait(submissionId: string, timeoutMs: number): Promise<string | null> {
    const submission = await this.harness.submission(submissionId as never, context);
    if (!submission) {
      throw new Error(`submission ${submissionId} not found`);
    }
    const settled = await Promise.race([
      submission.wait(context),
      new Promise<{ status: "running" }>((resolve) => {
        setTimeout(() => resolve({ status: "running" }), timeoutMs);
      }),
    ]) as { status: string; conversationId?: string; answer?: string };

    if (settled.status !== "done" || !settled.answer || !settled.conversationId) {
      return null;
    }
    return await this.readAnswerText(settled.conversationId, settled.answer);
  }

  /** 从 conversation transcript 读 answer entry 的 assistant 消息文本 */
  private async readAnswerText(conversationId: string, answerEntryId: string): Promise<string | null> {
    const conversation = await this.harness.conversation(conversationId as never, context);
    if (!conversation) return null;
    // answer 是最近的 entry——降序翻一页足够
    const page = await conversation.entries({ order: "descending" } as never, 50, undefined, context);
    for (const e of page.items ?? []) {
      if (String(e.id) !== String(answerEntryId)) continue;
      const model = (e as { model?: Array<{ content?: unknown }> }).model;
      const first = model?.[0];
      if (!first) return null;
      const content = first.content;
      if (typeof content === "string") return content;
      if (Array.isArray(content)) {
        return content
          .filter((b): b is { type: string; text?: string } => typeof b === "object" && b !== null)
          .map((b) => (b.type === "text" ? (b.text ?? "") : ""))
          .join("");
      }
      return null;
    }
    return null;
  }
}
