/**
 * pi-ai Models 配置（M7）：AIGC OpenAI 兼容中转，供 IntentConversation 的 GenerationTask 使用。
 * 写法仿 wenshu services/pi-agent/src/providers.ts（pi-ai 1.3.0 与 0.84.2 该接口兼容）。
 * PipelineTask/SkillTask 不调模型，不受此配置影响。
 */
import { createModels, createProvider, type Models, type ApiKeyAuth } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";

export const INTENT_PROVIDER_ID = "shanglv";

/** 固定 key 的 auth（环境变量优先，缺省用内置值——与 Python config.py 同源）。
 *  返回形状与 envApiKeyAuth 的 resolve 完全同构（auth: { apiKey }）。 */
function literalKeyAuth(key: string): ApiKeyAuth {
  return {
    name: "AIGC api key",
    resolve: async () => (key ? { auth: { apiKey: key }, source: "builtin" } : undefined),
  };
}

export function buildModels(): Models {
  const baseUrl = process.env.AIGC_BASE_URL ?? "";  // 与 config.py 的 LLM_CONFIG 同源
  const apiKey = process.env.AIGC_API_KEY ?? process.env.AIGC_APP_ID ?? "";
  const modelId = process.env.INTENT_MODEL ?? "deepseek-v4-flash";

  const models = createModels();
  models.setProvider(
    createProvider({
      id: INTENT_PROVIDER_ID,
      name: "shanglv AIGC relay",
      baseUrl,
      auth: { apiKey: literalKeyAuth(apiKey) },
      models: [
        {
          id: modelId,
          name: modelId,
          api: "openai-completions",
          provider: INTENT_PROVIDER_ID,
          baseUrl,
          reasoning: false,
          input: ["text"],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 128_000,
          maxTokens: 16_384,
          // 中转站兼容性显式写全（wenshu 同款套路）：非标 OpenAI 服务的已知差异
          compat: {
            supportsDeveloperRole: false,
            supportsReasoningEffort: false,
            supportsStore: false,
            supportsUsageInStreaming: true,
            maxTokensField: "max_tokens",
          },
        },
      ],
      api: openAICompletionsApi(),
    }),
  );
  return models;
}

/** 意图识别 conversation 的 ModelRef（名字引用，运行时由 Models 解析） */
export function intentModelRef(): { provider: string; modelId: string } {
  return { provider: INTENT_PROVIDER_ID, modelId: process.env.INTENT_MODEL ?? "deepseek-v4-flash" };
}

/** skill 生成 conversation 的 ModelRef（与 Python LLM_CONFIG 同源） */
export function skillModelRef(): { provider: string; modelId: string } {
  return { provider: INTENT_PROVIDER_ID, modelId: process.env.SKILL_MODEL ?? process.env.INTENT_MODEL ?? "deepseek-v4-flash" };
}
