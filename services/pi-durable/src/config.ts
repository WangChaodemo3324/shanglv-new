/**
 * pi-durable 服务配置：全部来自环境变量（与 Python 侧 PI_DURABLE_CONFIG 对齐）
 */
export interface Config {
  port: number;
  host: string;
  /** SQLite 存储文件（checkpoint/恢复的落盘层） */
  storagePath: string;
  /** Python FastAPI 基址（skill 回调目标） */
  pyBaseUrl: string;
  /** Python→pi-durable 的 X-Internal-Token（未配置时受保护路由 503） */
  internalToken: string;
  /** pi-durable→Python 回调的 X-Internal-Token（与 Python SKILL_EXECUTE_TOKEN 一致） */
  skillCallbackToken: string;
  /** 单次 skill 回调 HTTP 超时 */
  skillHttpTimeoutMs: number;
  /** skill 回调失败重试次数（总尝试 = 1 + retries） */
  skillHttpRetries: number;
  /** wait 端点单次长轮询上限 */
  waitMaxMs: number;
}

function num(name: string, fallback: number): number {
  const raw = process.env[name];
  const v = raw === undefined ? NaN : Number(raw);
  return Number.isFinite(v) ? v : fallback;
}

export function loadConfig(): Config {
  return {
    port: num("PORT", 8788),
    host: process.env.HOST ?? "127.0.0.1",
    storagePath: process.env.STORAGE_PATH ?? "../../data/pi-durable/session.sqlite",
    pyBaseUrl: (process.env.PY_BASE_URL ?? "http://127.0.0.1:8000").replace(/\/$/, ""),
    internalToken: process.env.INTERNAL_TOKEN ?? "shanglv-local",
    skillCallbackToken: process.env.SKILL_CALLBACK_TOKEN ?? "shanglv-local",
    skillHttpTimeoutMs: num("SKILL_HTTP_TIMEOUT_MS", 180_000),
    skillHttpRetries: num("SKILL_HTTP_ATTEMPTS", 2) - 1,
    waitMaxMs: num("WAIT_MAX_MS", 300_000),
  };
}
