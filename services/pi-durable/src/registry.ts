/**
 * Registry 装配 + RequestIndex 文档（requestId → taskId 原子去重索引）。
 * 文档不在 extension 里声明（durable 的 doc 注册路径是 conversationCreated 钩子，
 * 与内置 pi.* 文档同一方式——harness.ts 里装配）。
 */
import { createRegistry, defineExtension, defineDoc } from "@earendil-works/pi-durable";
import { PipelineTask, SkillTask } from "./tasks/pipeline.js";
import { SkillGenTask } from "./tasks/skill_gen.js";

/** root conversation 上的 requestId → {taskId, createdAt} 索引。
 *  键为 `${userId}:${requestId}`（与 Python 幂等键同源，跨用户不撞）。
 *  atomicity：读索引→建任务→写索引在 root.commit 的同一次事务内完成。 */
export const RequestIndex = defineDoc({
  kind: "shanglv.request_index",
  version: 1,
  scope: "conversation",
  history: "latest",
  fork: "current",
  initial: () => ({
    entries: {} as Record<string, { taskId: string; createdAt: number }>,
  }),
});

/** 索引保留上限（防止无限增长；超出后淘汰最旧的） */
export const REQUEST_INDEX_LIMIT = 1000;

export function buildRegistry() {
  const registry = createRegistry();
  registry.install(
    defineExtension({
      name: "shanglv.pipeline.core",
      tasks: [PipelineTask, SkillTask, SkillGenTask],
    }),
  );
  return registry;
}
