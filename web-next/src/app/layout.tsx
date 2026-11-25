import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "商旅助手 · 多智能体差旅规划",
  description: "差旅政策问答、偏好记忆与行程规划的多智能体助手",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
