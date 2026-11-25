"use client";

import { useEffect, useState } from "react";
import styles from "./page.module.css";

/* ============ 类型（与各 skill 输出契约一致） ============ */
export interface ExpenseData {
  query?: string;
  budget_cap?: { accommodation?: number; meals?: number; total?: number };
  estimate?: {
    accommodation?: { range: [number, number]; basis?: string };
    meals?: { range: [number, number]; basis?: string };
    intercity_transport?: { range: [number, number]; basis?: string };
    local_transport?: { range: [number, number]; basis?: string };
    total?: { range: [number, number] };
  };
  over_budget: boolean;
  over_amount?: { range: [number, number] };
  warnings: string[];
  suggestions: string[];
}

export interface ConflictData {
  checked_trip?: { destination: string; start_date: string; end_date: string } | null;
  trips?: { destination: string; start_date: string; end_date: string; origin?: string }[] | null;
  conflicts: { severity?: string; conflict_with?: string; description?: string }[];
  warnings: { description?: string }[] | null;
  result: string;
}

export interface CompareData {
  comparison_target?: string;
  options?: { name: string; price_range: [number, number]; duration?: string; pros?: string; cons?: string }[];
  recommendation?: string;
  recommendation_reason?: string;
}

export interface WeatherData {
  destination?: string;
  weather_summary?: string;
  alerts?: { category: string; advice: string }[];
  overall_risk?: string;
}

export interface InsightsPayload {
  expense?: ExpenseData | null;
  conflict?: ConflictData | null;
  compare?: CompareData | null;
  weather?: WeatherData | null;
}

export default function InsightsPage() {
  const [data, setData] = useState<InsightsPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    // key 参数来自卡片链接（消息粒度，多轮不覆盖）；无参数兜底读 latest
    const key = params.get("key") || "latest";
    const raw = localStorage.getItem(`insights:${key}`)
      ?? localStorage.getItem("insights:latest");
    if (raw) {
      try { setData(JSON.parse(raw)); } catch { /* ignore */ }
    }
    setLoading(false);
  }, []);

  if (loading) return <div className={styles.loading}>加载中…</div>;
  if (!data || !Object.values(data).some(Boolean)) {
    return (
      <div className={styles.empty}>
        <h2>暂无数据</h2>
        <p>在 <a href="/">对话页</a> 问费用预估、行程冲突、比价或出行建议类问题后，这里会展示可视化图表。</p>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.brand}><span className={styles.logo}>📊</span>
          <div><h1>数据洞察</h1><small>费用 · 冲突 · 比价 · 建议</small></div>
        </div>
        <a className={styles.back} href="/">← 返回对话</a>
      </header>

      <main className={styles.main}>
        {data.expense && <ExpenseChart d={data.expense} />}
        {data.conflict && <ConflictGantt d={data.conflict} />}
        {data.compare && <CompareCards d={data.compare} />}
        {data.weather && <WeatherCards d={data.weather} />}
      </main>
    </div>
  );
}

/* ============ 1. 费用预估：区间条形图 vs 预算上限线 ============ */
function ExpenseChart({ d }: { d: ExpenseData }) {
  if (!d?.budget_cap || !d?.estimate) return null;
  const items: { name: string; range: [number, number]; cap: number | null; basis?: string }[] = [
    { name: "住宿", range: d.estimate.accommodation?.range || [0, 0], cap: d.budget_cap?.accommodation ?? null, basis: d.estimate.accommodation?.basis },
    { name: "餐饮", range: d.estimate.meals?.range || [0, 0], cap: d.budget_cap?.meals ?? null, basis: d.estimate.meals?.basis },
    { name: "城际交通", range: d.estimate.intercity_transport?.range || [0, 0], cap: null, basis: d.estimate.intercity_transport?.basis },
    { name: "市内交通", range: d.estimate.local_transport?.range || [0, 0], cap: null, basis: d.estimate.local_transport?.basis },
  ].filter(i => i.range[1] > 0);

  const max = Math.max(...items.map(i => Math.max(i.range[1], i.cap || 0)), 1);
  const fmt = (n: number) => `¥${n}`;

  return (
    <section className={styles.card}>
      <h3>💰 费用预估 vs 公司上限</h3>
      {d.over_budget && <div className={styles.alertBar}>⚠️ 预估超出住宿+餐饮上限（交通实报实销不在此列）</div>}
      <div className={styles.chart}>
        {items.map(it => {
          const over = it.cap != null && it.range[0] > it.cap;
          return (
            <div key={it.name} className={styles.chartRow}>
              <span className={styles.rowLabel}>{it.name}</span>
              <div className={styles.rowTrack}>
                {/* 预算上限竖线 */}
                {it.cap != null && (
                  <div className={styles.capLine} style={{ left: `${Math.min(it.cap / max, 1) * 100}%` }} title={`上限 ${fmt(it.cap)}`} />
                )}
                {/* 预估区间条 */}
                <div className={`${styles.rangeBar} ${over ? styles.overBar : ""}`}
                  style={{ left: `${it.range[0] / max * 100}%`, width: `${(it.range[1] - it.range[0]) / max * 100}%` }} />
                {/* 区间端点数值 */}
                <span className={styles.rangeText} style={{ left: `${Math.min(it.range[1] / max, 1) * 100}%` }}>
                  {fmt(it.range[0])}-{fmt(it.range[1])}
                </span>
              </div>
              {it.cap != null && <span className={over ? styles.capOver : styles.capOk}>{fmt(it.cap)}</span>}
            </div>
          );
        })}
      </div>
      <div className={styles.legend}>
        <span><i className={styles.rangeDot} /> 预估区间</span>
        <span><i className={styles.capDot} /> 公司上限</span>
      </div>
      {(d.warnings?.length || 0) > 0 && (
        <ul className={styles.warnList}>{d.warnings.map((w, i) => <li key={i}>⚠️ {w}</li>)}</ul>
      )}
      {(d.suggestions?.length || 0) > 0 && (
        <ul className={styles.suggList}>{d.suggestions.map((s, i) => <li key={i}>💡 {s}</li>)}</ul>
      )}
    </section>
  );
}

/* ============ 2. 冲突检测：甘特时间条 ============ */
function ConflictGantt({ d }: { d: ConflictData }) {
  /* 已有行程来源：优先 itinerary_lookup 提供的 trips；
     缺失时从 conflicts 描述提取（"上海出差 2027-03-21 至 2027-03-23" 格式） */
  const existing: { label: string; start: string; end: string }[] = [];
  (d.trips || []).forEach(t => {
    if (t.start_date && t.end_date) {
      existing.push({ label: t.origin ? `${t.origin}→${t.destination}` : (t.destination || ""), start: t.start_date, end: t.end_date });
    }
  });
  if (!existing.length) {
    (d.conflicts || []).forEach(c => {
      const m = (c.conflict_with || "").match(/([\u4e00-\u9fa5]+)[^\d]*(\d{4}-\d{2}-\d{2})[^\d]*(\d{4}-\d{2}-\d{2})/);
      if (m) existing.push({ label: m[1], start: m[2], end: m[3] });
    });
  }

  if (!d.checked_trip?.start_date) return null;   // 数据不完整时不渲染本区块
  const checked = d.checked_trip;
  const all = [
    { label: `新计划 · ${d.checked_trip.destination}`, start: d.checked_trip.start_date, end: d.checked_trip.end_date, isNew: true },
    ...existing.map(t => ({ ...t, isNew: false })),
  ].filter(t => t.start && t.end);

  const toTs = (s: string) => new Date(s).getTime();
  const min = Math.min(...all.map(t => toTs(t.start)));
  const max = Math.max(...all.map(t => toTs(t.end)));
  const span = Math.max(max - min, 86400000); // 至少一天
  const pad = span * 0.06;

  const conflictRows = new Set<number>();
  all.forEach((t, i) => {
    if (!t.isNew) {
      const overlap = toTs(t.start) <= toTs(checked.end_date) && toTs(t.end) >= toTs(checked.start_date);
      if (overlap) conflictRows.add(i);
    }
  });

  const riskColor = d.result === "conflict" ? "#dc2626" : d.result === "warning" ? "#d97706" : "#059669";
  const riskLabel = d.result === "conflict" ? "存在冲突" : d.result === "warning" ? "有风险提示" : "无冲突";

  return (
    <section className={styles.card}>
      <h3>🗓️ 行程冲突检测 <span className={styles.riskBadge} style={{ background: riskColor }}>{riskLabel}</span></h3>
      <div className={styles.gantt}>
        {all.map((t, i) => {
          const left = (toTs(t.start) - min + pad) / (span + pad * 2) * 100;
          const width = (toTs(t.end) - toTs(t.start)) / (span + pad * 2) * 100;
          return (
            <div key={i} className={styles.ganttRow}>
              <span className={styles.rowLabel}>{t.label}</span>
              <div className={styles.ganttTrack}>
                <div className={`${styles.ganttBar} ${t.isNew ? styles.newBar : conflictRows.has(i) ? styles.clashBar : styles.oldBar}`}
                  style={{ left: `${left}%`, width: `${Math.max(width, 2)}%` }}>
                  <small>{t.start.slice(5)} ~ {t.end.slice(5)}</small>
                </div>
              </div>
            </div>
          );
        })}
      </div>
      {d.conflicts?.map((c, i) => (
        <div key={i} className={styles.conflictItem}>❌ {c.severity ? <b>[{c.severity}]</b> : null} {c.description || ""}{c.conflict_with ? `（与 ${c.conflict_with}）` : ""}</div>
      ))}
      {d.warnings?.map((w, i) => (
        <div key={i} className={styles.warnItem}>⚠️ {w.description || ""}</div>
      ))}
    </section>
  );
}

/* ============ 3. 比价：对比卡片组 ============ */
function CompareCards({ d }: { d: CompareData }) {
  if (!d?.options?.length) return null;
  const maxPrice = Math.max(...d.options.map(o => o.price_range[1]), 1);
  return (
    <section className={styles.card}>
      <h3>🔀 {d.comparison_target} · 方案对比</h3>
      <div className={styles.compareGrid}>
        {d.options.map((o, i) => {
          const rec = o.name === d.recommendation;
          return (
            <div key={i} className={`${styles.compareCard} ${rec ? styles.recCard : ""}`}>
              {rec && <span className={styles.recBadge}>✓ 推荐</span>}
              <b className={styles.compareName}>{o.name}</b>
              {/* 价格区间可视化 */}
              <div className={styles.priceTrack}>
                <div className={styles.priceBar}
                  style={{ left: `${o.price_range[0] / maxPrice * 100}%`, width: `${(o.price_range[1] - o.price_range[0]) / maxPrice * 100}%` }} />
              </div>
              <span className={styles.priceText}>¥{o.price_range[0]} - ¥{o.price_range[1]}</span>
              <span className={styles.durationText}>⏱ {o.duration}</span>
              <p className={styles.pros}>✓ {o.pros}</p>
              <p className={styles.cons}>✗ {o.cons}</p>
            </div>
          );
        })}
      </div>
      {d.recommendation_reason && <p className={styles.recReason}>推荐理由：{d.recommendation_reason}</p>}
    </section>
  );
}

/* ============ 4. 天气建议：分类卡片 ============ */
const WEATHER_META: Record<string, { icon: string; name: string }> = {
  packing: { icon: "🎒", name: "行装" }, transport: { icon: "🚄", name: "交通" },
  schedule: { icon: "📅", name: "行程" }, health: { icon: "💊", name: "健康" },
};
function WeatherCards({ d }: { d: WeatherData }) {
  if (!d?.alerts?.length) return null;
  const riskColor = d.overall_risk === "高" ? "#dc2626" : d.overall_risk === "中" ? "#d97706" : "#059669";
  return (
    <section className={styles.card}>
      <h3>🌦️ {d.destination} · 出行建议 <span className={styles.riskBadge} style={{ background: riskColor }}>风险 {d.overall_risk}</span></h3>
      <p className={styles.weatherSummary}>{d.weather_summary}</p>
      <div className={styles.weatherGrid}>
        {d.alerts.map((a, i) => {
          const m = WEATHER_META[a.category] || { icon: "📌", name: a.category };
          return (
            <div key={i} className={styles.weatherCard}>
              <span className={styles.weatherIcon}>{m.icon}</span>
              <b>{m.name}</b>
              <p>{a.advice}</p>
            </div>
          );
        })}
      </div>
    </section>
  );
}
