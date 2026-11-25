/**
 * SkillConversations（M8）：skill 生成用的短命 conversation 池。
 *
 * 与 IntentConversations（per-user 长存）不同：skill 的 prompt 必须是唯一事实源——
 * conversation 里不能有历史轮次泄漏（会破坏确定性行为），且 8 个 P1 并行 skill
 * 不能共享 conversation（busy 会串行化）。因此每个 (turn, skill, stage) 用一个
 * 全新 ownerless conversation，一次性使用。
 *
 * 崩溃幂等：conversationId + requestId 先落 SkillGenTask checkpoint 再 submit——
 * invocation 结束 wait 被拒 → 任务重放该 phase → 同 requestId submit 返回既有
 * submission（conversation 级去重），LLM 不会双跑。
 */
import type { Harness, Conversation } from "@earendil-works/pi-durable";
import { context } from "./harness.js";
import { skillModelRef } from "./providers.js";

export class SkillConversations {
  constructor(private readonly harness: Harness) {}

  /** 创建一个全新的短命 conversation（模型固定 skill 模型，无 instructions） */
  async create(): Promise<Conversation> {
    return await this.harness.createConversation(
      { ownership: { kind: "ownerless" }, agent: { model: skillModelRef(), instructions: "" } },
      context,
    );
  }

  /** 按 id 取完整 Conversation（有 entries 读取能力——ConversationHandle 只有 submit/abort/waitForIdle） */
  async getConversation(conversationId: string): Promise<Conversation | undefined> {
    return await this.harness.conversation(conversationId as never, context);
  }

  /** 在指定 conversation 上提交生成 prompt（requestId 幂等：重放返回既有 submission） */
  async submit(conversation: Conversation, requestId: string, prompt: string): Promise<string> {
    const submission = await conversation.submit(
      { type: "input", content: prompt, requestId },
      context,
    );
    return String(submission.id);
  }

  /** 读 conversation transcript 中 answer entry 的 assistant 文本 */
  async readAnswerText(conversation: Conversation, answerEntryId: string): Promise<string | null> {
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
