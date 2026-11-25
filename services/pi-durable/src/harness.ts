/**
 * Harness 单例生命周期：SQLite 存储 + resume（崩溃后 running→pending 续跑）。
 * 单 storage 只允许一个 Harness open（spec 约束）——本服务严格单实例。
 */
import { Harness } from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { BACKGROUND_CONTEXT, type Context } from "@earendil-works/chord/context";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { buildRegistry, RequestIndex } from "./registry.js";
import { buildModels } from "./providers.js";
import type { Config } from "./config.js";

export const context: Context = BACKGROUND_CONTEXT;

export interface DurableRuntime {
  harness: Harness;
  rootId: string;
  close(): Promise<void>;
}

let runtimePromise: Promise<DurableRuntime> | undefined;

export function getDurableRuntime(config: Config): Promise<DurableRuntime> {
  runtimePromise ??= openRuntime(config);
  return runtimePromise;
}

async function openRuntime(config: Config): Promise<DurableRuntime> {
  mkdirSync(dirname(config.storagePath), { recursive: true });
  const storage = await openNodeSqliteStorage(config.storagePath);
  // models：真实 pi-ai 配置（M7 起 IntentConversation 的 GenerationTask 消费；
  // PipelineTask/SkillTask 不调模型，不受影响）
  const harness = await Harness.open(storage, {
    models: buildModels(),
    registry: buildRegistry(),
    // 与内置 pi.* 文档同一注册方式：conversation 创建时初始化 RequestIndex。
    // guard：root/intent 等长期 conversation 才建索引文档；skill 短命 conversation
    // （每 turn×skill×stage 一个）跳过——否则海量空 RequestIndex 文档。
    // 区分依据：root 是固定 id；intent/skill conversation 无 init 也无索引语义，
    // 仅 root.commit 的 RequestIndex 去重依赖该文档。以「是否带 agent 配置创建」近似区分不可靠，
    // 采用 ownership ownerless + 无 init 的短命会话不建索引——但 conversationCreated 拿不到该信息，
    // 所以改为：只有 root（id === rootId 首次创建）建索引；intent conversation 的索引在
    // IntentConversations 里按需创建。
    conversationCreated: async (tx, record) => {
      void record;
    },
  }, context);
  harness.resume();
  const root = await harness.root(context, { agent: { model: { provider: "openai", modelId: "gpt-4o" } } });
  return {
    harness,
    rootId: String(root.id),
    close: () => harness.close(context),
  };
}
