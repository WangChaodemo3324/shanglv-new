/**
 * 启动编排：config → harness（open + resume）→ Fastify listen；SIGTERM 时 harness.close()。
 * 注意：同一 session.sqlite 永远只许一个进程 open（单 storage 单 Harness）。
 */
import { loadConfig } from "./config.js";
import { getDurableRuntime } from "./harness.js";
import { buildApp } from "./server.js";
import { setPythonClient } from "./tasks/pipeline.js";
import { setSkillGenDeps } from "./tasks/skill_gen.js";
import { SkillConversations } from "./skill_conversations.js";
import { PythonClient } from "./python_client.js";

const config = loadConfig();
const runtime = await getDurableRuntime(config);
setPythonClient(new PythonClient(config));
setSkillGenDeps(new SkillConversations(runtime.harness), new PythonClient(config));
const app = buildApp(config, runtime);

await app.listen({ port: config.port, host: config.host });
console.log(`✓ shanglv pi-durable listening on ${config.host}:${config.port}`);
console.log(`  storage: ${config.storagePath} (root conversation ${runtime.rootId})`);
console.log(`  python callback: ${config.pyBaseUrl}/internal/skills/{name}/execute`);

const shutdown = async (signal: string) => {
  console.log(`\n${signal} received, closing harness...`);
  try {
    await app.close();
    await runtime.close();
    console.log("✓ closed cleanly");
  } catch (err) {
    console.error("close failed:", err);
  }
  process.exit(0);
};
process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));

// Harness Session 失败（存储层致命错误）时自杀重启，由进程管理器拉起后 resume 续跑
runtime.harness.closed.then((end) => {
  console.error("Harness session ended:", JSON.stringify(end));
  process.exit(1);
});
