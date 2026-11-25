"use client";

import { useEffect, useState } from "react";
import Chat from "@/components/Chat";

export default function Home() {
  const [ready, setReady] = useState(false);
  useEffect(() => { setReady(true); }, []);
  if (!ready) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", color: "#6b7280" }}>
        加载中…
      </div>
    );
  }
  return <Chat />;
}
