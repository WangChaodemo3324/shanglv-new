import { Suspense } from "react";

export default function TripLayout({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<p>正在加载行程…</p>}>{children}</Suspense>;
}
