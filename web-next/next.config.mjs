/** @type {import('next').NextConfig} */
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

const nextConfig = {
  // Windows local builds do not require symlink privileges. Linux/Docker
  // retains the standalone bundle used by the existing Dockerfile.
  output: process.platform === "win32" ? undefined : "standalone",
  experimental: {
    // LLM 问答（意图识别+多skill+行程规划）端到端可达 3-8 分钟，代理超时放宽到 10 分钟
    proxyTimeout: 600000,
  },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${BACKEND_URL}/:path*` },
    ];
  },
};

export default nextConfig;
