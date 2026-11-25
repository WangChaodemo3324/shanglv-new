import { Suspense } from "react";

export default function InsightsLayout({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<p>正在加载分析…</p>}>{children}</Suspense>;
}
