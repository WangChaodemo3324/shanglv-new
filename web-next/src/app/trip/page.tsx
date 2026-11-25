"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import styles from "./page.module.css";

/* maplibre 经 CDN script 加载（绕开 webpack 对 web worker 的处理） */
/* 本地自托管（public/），同源加载无跨域拦截 */
const MAPLIBRE_CDN = "/maplibre-gl.js";
const MAPLIBRE_CSS = "/maplibre-gl.css";

declare global { interface Window { maplibregl?: any } }

function loadMapLibre(): Promise<any> {
  if (typeof window === "undefined") return Promise.resolve(null);
  if (window.maplibregl) return Promise.resolve(window.maplibregl);
  return new Promise((resolve) => {
    if (!document.querySelector(`link[href="${MAPLIBRE_CSS}"]`)) {
      const link = document.createElement("link");
      link.rel = "stylesheet"; link.href = MAPLIBRE_CSS;
      document.head.appendChild(link);
    }
    const script = document.createElement("script");
    script.src = MAPLIBRE_CDN;
    script.onload = () => resolve((window as any).maplibregl);
    document.head.appendChild(script);
  });
}

/* ============ 类型（与后端 itinerary 契约一致） ============ */
interface Activity {
  time: string; location: string; description?: string; transport?: string;
  lat?: number | null; lng?: number | null;
  coordinate_system?: string;
}
interface DayPlan {
  day: number; date?: string; city?: string; theme?: string;
  activities: Activity[]; meals?: { lunch?: string; dinner?: string };
}
interface Itinerary {
  title?: string; duration?: string; route?: string;
  daily_plans?: DayPlan[]; accommodation?: string; tips?: string[];
  solver_summary?: string;
}

const DAY_COLORS = ["#e11d48", "#2563eb", "#059669", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d"];

/* WGS-84 → GCJ-02 坐标转换（高德底图为火星坐标系，WGS 点需偏移校正） */
const PI = Math.PI, A = 6378245.0, EE = 0.00669342162296594323;
function transformLat(x: number, y: number): number {
  let r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  r += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
  r += (20.0 * Math.sin(y * PI) + 40.0 * Math.sin(y / 3.0 * PI)) * 2.0 / 3.0;
  r += (160.0 * Math.sin(y / 12.0 * PI) + 320 * Math.sin(y * PI / 30.0)) * 2.0 / 3.0;
  return r;
}
function transformLng(x: number, y: number): number {
  let r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  r += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
  r += (20.0 * Math.sin(x * PI) + 40.0 * Math.sin(x / 3.0 * PI)) * 2.0 / 3.0;
  r += (150.0 * Math.sin(x / 12.0 * PI) + 300.0 * Math.sin(x / 30.0 * PI)) * 2.0 / 3.0;
  return r;
}
function wgs2gcj(lng: number, lat: number): [number, number] {
  let dLat = transformLat(lng - 105.0, lat - 35.0);
  let dLng = transformLng(lng - 105.0, lat - 35.0);
  const radLat = lat / 180.0 * PI;
  let magic = Math.sin(radLat);
  magic = 1 - EE * magic * magic;
  const sqrtMagic = Math.sqrt(magic);
  dLat = (dLat * 180.0) / ((A * (1 - EE)) / (magic * sqrtMagic) * PI);
  dLng = (dLng * 180.0) / (A / sqrtMagic * Math.cos(radLat) * PI);
  return [lng + dLng, lat + dLat];
}
const TABS = ["概览", "行程", "住宿"] as const;

export default function TripPage() {
  const params = useSearchParams();
  const [itinerary, setItinerary] = useState<Itinerary | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]>("概览");
  const [activeDay, setActiveDay] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);

  /* 数据来源：localStorage（聊天页存入）兜底 URL 参数 */
  useEffect(() => {
    // key 参数来自卡片链接（消息粒度，多轮不覆盖）；无参数兜底读 latest
    const key = params.get("key") || "latest_itinerary";
    const raw = localStorage.getItem(`trip:${key}`)
      ?? localStorage.getItem("trip:latest_itinerary");
    if (raw) {
      try { setItinerary(JSON.parse(raw)); } catch { /* ignore */ }
    }
    setLoading(false);
  }, [params]);

  const days = itinerary?.daily_plans || [];
  const dayPoints = useMemo(() => days.map((d) => ({
    day: d.day,
    color: DAY_COLORS[(d.day - 1) % DAY_COLORS.length],
    // WGS-84 → GCJ-02（高德底图坐标系），保证标记落在正确位置
    points: (d.activities || [])
      .filter(a => typeof a.lat === "number" && typeof a.lng === "number")
      .map(a => {
        const [glng, glat] = a.coordinate_system === "GCJ-02"
          ? [a.lng as number, a.lat as number]
          : wgs2gcj(a.lng as number, a.lat as number);
        return { ...a, lat: glat, lng: glng } as Activity & { lat: number; lng: number };
      }),
  })), [days]);

  if (loading) return <div className={styles["tp-loading"]}>加载行程数据…</div>;
  if (!itinerary || !days.length) {
    return (
      <div className={styles["tp-empty"]}>
        <h2>暂无行程数据</h2>
        <p>先在 <a href="/">对话页</a> 请求一次行程规划（如「帮我规划10月20日到22日去杭州的3天行程」），完成后这里会展示可视化手册。</p>
      </div>
    );
  }

  return (
    <div className={styles["tp-page"]}>
      {/* 顶栏 */}
      <header className={styles["tp-header"]}>
        <div className={styles["tp-brand"]}>
          <span className={styles["tp-logo"]}>✈️</span>
          <div>
            <h1>{itinerary.title || "行程手册"}</h1>
            <small>{itinerary.duration} · {itinerary.route}{activeDay !== null ? ` · 当前 D${activeDay}` : ""}</small>
          </div>
        </div>
        <nav className={styles["tp-tabs"]}>
          {TABS.map(t => (
            <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>
          ))}
        </nav>
        <a className={styles["tp-back"]} href="/">← 返回对话</a>
      </header>

      <main className={styles["tp-main"]}>
        {tab === "概览" && (
          <OverviewTab itinerary={itinerary} dayPoints={dayPoints} activeDay={activeDay} setActiveDay={setActiveDay} />
        )}
        {tab === "行程" && (
          <TimelineTab days={days} activeDay={activeDay} setActiveDay={setActiveDay} />
        )}
        {tab === "住宿" && (
          <StayTab itinerary={itinerary} />
        )}
      </main>
    </div>
  );
}

/* ============ 概览 Tab：地图 + 日期选择 ============ */
function OverviewTab({ itinerary, dayPoints, activeDay, setActiveDay }: {
  itinerary: Itinerary;
  dayPoints: { day: number; color: string; points: (Activity & { lat: number; lng: number })[] }[];
  activeDay: number | null; setActiveDay: (d: number | null) => void;
}) {
  return (
    <div className={styles["tp-overview"]}>
      <TripMapView dayPoints={dayPoints} activeDay={activeDay} />
      <aside className={styles["tp-side"]}>
        <h3>路线总览</h3>
        <div className={styles["tp-daylist"]}>
          {dayPoints.map(d => (
            <button key={d.day} className={activeDay === d.day ? "on" : ""}
              style={{ ["--c" as string]: d.color }}
              onClick={() => setActiveDay(activeDay === d.day ? null : d.day)}>
              <i className={"dot"} />
              <b>D{d.day}</b>
              <small>{d.points.length} 个地点</small>
            </button>
          ))}
        </div>
        {itinerary.solver_summary && <p className={styles["tp-solver"]}>{itinerary.solver_summary}</p>}
        <p className={styles["tp-hint"]}>点击日期筛选路线；再次点击取消</p>
      </aside>
    </div>
  );
}

/* ============ 地图组件（概览共享） ============ */
function TripMapView({ dayPoints, activeDay }: {
  dayPoints: { day: number; color: string; points: (Activity & { lat: number; lng: number })[] }[];
  activeDay: number | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const mapRef = useRef<any>(null);
  const [ready, setReady] = useState(false);

  const all = dayPoints.flatMap(d => d.points);
  const hasCoords = all.length > 0;

  useEffect(() => {
    if (!hasCoords || !ref.current || mapRef.current) return;
    let cancelled = false;
    setReady(false);   // StrictMode 重挂载时重置，避免旧 ready 让重画挂在已移除的 map 上
    loadMapLibre().then((ml) => {
      if (!ml || cancelled || !ref.current || mapRef.current) return;
      const lons = all.map(p => p.lng), lats = all.map(p => p.lat);
      const map = new ml.Map({
      container: ref.current,
      style: {
        version: 8,
        sources: { amap: { type: "raster", tiles: ["https://webst01.is.autonavi.com/appmaptile?style=7&x={x}&y={y}&z={z}", "https://webst02.is.autonavi.com/appmaptile?style=7&x={x}&y={y}&z={z}", "https://webst03.is.autonavi.com/appmaptile?style=7&x={x}&y={y}&z={z}", "https://webst04.is.autonavi.com/appmaptile?style=7&x={x}&y={y}&z={z}"], tileSize: 256, attribution: "© 高德地图" } },
        layers: [{ id: "amap", type: "raster", source: "amap" }],
      },
      center: [120.15, 30.25],   // 先给默认中心（杭州），load 后再 fitBounds 到行程范围
      zoom: 10,
      devicePixelRatio: window.devicePixelRatio || 1,   // 高分屏按实际像素比渲染，瓦片不糊
      });
      mapRef.current = map;
      map.on("load", () => {
        map.fitBounds(
          [Math.min(...lons) - 0.05, Math.min(...lats) - 0.05, Math.max(...lons) + 0.05, Math.max(...lats) + 0.05],
          { padding: 50, duration: 0 }
        );
        setReady(true);
      });
    });
    return () => {
      cancelled = true;
      mapRef.current?.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasCoords]);

  const markersRef = useRef<any[]>([]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    // 清理上一次的全部图层、数据源与标记（严格顺序：先层后源；marker 单独管理）
    map.getStyle().layers?.forEach((l: { id: string }) => {
      if (l.id.startsWith("route-d")) map.removeLayer(l.id);
    });
    dayPoints.forEach(d => {
      const sid = `src-d${d.day}`;
      if (map.getSource(sid)) map.removeSource(sid);
    });
    const ml = (window as any).maplibregl;
    if (!ml) return;
    markersRef.current.forEach(m => { try { m.remove(); } catch {} });
    markersRef.current = [];

    // 重画：每天的路线 + 标记
    dayPoints.forEach(d => {
      const lid = `route-d${d.day}`, sid = `src-d${d.day}`;
      const dim = activeDay !== null && activeDay !== d.day;
      if (d.points.length > 1) {
        map.addSource(sid, { type: "geojson", data: { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: d.points.map(p => [p.lng, p.lat]) } } });
        map.addLayer({ id: `${lid}-halo`, type: "line", source: sid, paint: { "line-color": "#ffffff", "line-width": 7, "line-opacity": dim ? 0 : 0.9 } });
        map.addLayer({ id: `${lid}-line`, type: "line", source: sid, paint: { "line-color": d.color, "line-width": 4, "line-opacity": dim ? 0 : 0.9, "line-dasharray": [2, 1.5] } });
      }
      d.points.forEach((p, pi) => {
        // 外层 wrapper 控制 dim 视觉（maplibre 会往元素 inline 写 opacity:1，不能用 opacity）
        const el = document.createElement("div");
        el.className = styles["tp-marker"];
        el.dataset.day = String(d.day);
        // 筛选时非选中天的标记直接隐藏（点大，变灰仍会互相遮挡看不清位置）
        el.dataset.dim = dim ? "1" : "0";
        const dot = document.createElement("div");
        dot.style.background = d.color;
        // 圆点内显示当天序号（1、2、3…），一眼看清游览顺序
        const label = document.createElement("span");
        label.textContent = String(pi + 1);
        dot.appendChild(label);
        el.appendChild(dot);
        const marker = new ml.Marker({ element: el })
          .setLngLat([p.lng, p.lat])
          .setPopup(new ml.Popup({ offset: 20 }).setHTML(
            `<div class="tp-popup"><b>D${d.day} · 第${pi + 1}站 · ${p.time}</b><span>${p.location}</span></div>`))
          .addTo(map);
        markersRef.current.push(marker);
      });
    });
  }, [ready, activeDay, dayPoints]);

  if (!hasCoords) return <div className={styles["tp-map-empty"]}>本次行程活动未包含坐标</div>;
  return <div ref={ref} className={styles["tp-map"]} />;
}

/* ============ 行程 Tab：每日时间轴 ============ */
function TimelineTab({ days, activeDay, setActiveDay }: {
  days: DayPlan[]; activeDay: number | null; setActiveDay: (d: number | null) => void;
}) {
  const [openDay, setOpenDay] = useState<number>(days[0]?.day ?? 1);
  return (
    <div className={styles["tp-timeline"]}>
      <div className={styles["tp-daytabs"]}>
        {days.map(d => (
          <button key={d.day} className={openDay === d.day ? "on" : ""}
            style={{ ["--c" as string]: DAY_COLORS[(d.day - 1) % DAY_COLORS.length] }}
            onClick={() => { setOpenDay(d.day); setActiveDay(d.day); }}>
            D{d.day}<small>{d.date?.slice(5) || ""}</small>
          </button>
        ))}
      </div>
      {days.filter(d => d.day === openDay).map(d => (
        <div key={d.day} className={styles["tp-daycard"]}>
          <div className={styles["tp-dayhead"]}>
            <h3>D{d.day} · {d.date} {d.city ? `· ${d.city}` : ""}</h3>
            {d.theme && <span className={styles["tp-theme"]}>{d.theme}</span>}
          </div>
          <div className={styles["tp-acts"]}>
            {d.activities.map((a, i) => (
              <div key={i} className={styles["tp-act"]}>
                <span className={styles["tp-acttime"]}>{a.time}</span>
                <div className={styles["tp-actbody"]}>
                  <b>{a.location}</b>
                  {a.description && <p>{a.description}</p>}
                  {a.transport && <small>🚇 {a.transport}</small>}
                </div>
              </div>
            ))}
          </div>
          {d.meals && (d.meals.lunch || d.meals.dinner) && (
            <div className={styles["tp-meals"]}>
              {d.meals.lunch && <span>🍜 午餐：{d.meals.lunch}</span>}
              {d.meals.dinner && <span>🍽️ 晚餐：{d.meals.dinner}</span>}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

/* ============ 住宿 Tab ============ */
function StayTab({ itinerary }: { itinerary: Itinerary }) {
  return (
    <div className={styles["tp-stay"]}>
      <div className={styles["tp-staycard"]}>
        <h3>🏨 住宿建议</h3>
        <p>{itinerary.accommodation || "见行程文本"}</p>
      </div>
      {itinerary.tips?.length ? (
        <div className={styles["tp-staycard"]}>
          <h3>💡 注意事项</h3>
          <ul>{itinerary.tips.map((t, i) => <li key={i}>{t}</li>)}</ul>
        </div>
      ) : null}
    </div>
  );
}
