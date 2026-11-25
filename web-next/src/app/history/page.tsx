"use client";

import { useEffect, useRef, useState } from "react";
import styles from "./page.module.css";

/* maplibre 经 CDN script 加载（与 /trip 同方案） */
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

/* WGS-84 → GCJ-02（高德底图坐标系） */
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

/* 常用城市坐标（WGS-84）——历史行程只有城市名，前端做地理编码 */
const CITY_COORDS: Record<string, [number, number]> = {
  北京: [116.407, 39.904], 上海: [121.473, 31.230], 广州: [113.264, 23.129], 深圳: [114.057, 22.543],
  杭州: [120.155, 30.274], 南京: [118.796, 32.060], 成都: [104.066, 30.572], 武汉: [114.305, 30.593],
  西安: [108.940, 34.341], 苏州: [120.585, 31.299], 天津: [117.190, 39.125], 重庆: [106.551, 29.563],
  厦门: [118.089, 24.479], 青岛: [120.382, 36.067], 大连: [121.614, 38.914], 宁波: [121.550, 29.874],
  无锡: [120.311, 31.491], 长沙: [112.938, 28.228], 郑州: [113.625, 34.746], 济南: [117.120, 36.651],
  哈尔滨: [126.642, 45.756], 沈阳: [123.431, 41.805], 昆明: [102.832, 24.880], 合肥: [117.227, 31.820],
  福州: [119.296, 26.074], 石家庄: [114.514, 38.042], 南昌: [115.858, 28.682], 贵阳: [106.630, 26.647],
  太原: [112.548, 37.870], 南宁: [108.366, 22.817], 兰州: [103.834, 36.061], 乌鲁木齐: [87.617, 43.793],
};

interface Trip {
  origin?: string | null; destination?: string | null;
  start_date?: string | null; end_date?: string | null; purpose?: string | null;
}

interface Prefetch { history: Trip[] }

export default function HistoryPage() {
  const [trips, setTrips] = useState<Trip[] | null>(null);
  const [userId, setUserId] = useState("default_user");
  const mapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const saved = localStorage.getItem("shanglv_uid");
    if (saved) setUserId(saved);
    fetch(`/api/users/${encodeURIComponent(saved || "default_user")}/history?limit=50`)
      .then(r => r.json())
      .then((d: Prefetch) => setTrips(d.history || []))
      .catch(() => setTrips([]));
  }, []);

  /* 有坐标的行程（按目的地聚合去重画线） */
  const geoTrips = (trips || []).filter(t => {
    const c = CITY_COORDS[t.destination || ""];
    return c != null;
  });
  const destCount: Record<string, number> = {};
  geoTrips.forEach(t => { destCount[t.destination!] = (destCount[t.destination!] || 0) + 1; });
  const ranking = Object.entries(destCount).sort((a, b) => b[1] - a[1]);

  useEffect(() => {
    if (!geoTrips.length || !mapRef.current) return;
    let cancelled = false;
    loadMapLibre().then((ml) => {
      if (!ml || cancelled || !mapRef.current) return;
      const points = geoTrips.map(t => {
        const [lng, lat] = CITY_COORDS[t.destination!];
        return { ...t, gcj: wgs2gcj(lng, lat) };
      });
      const lngs = points.map(p => p.gcj[0]), lats = points.map(p => p.gcj[1]);
      const map = new ml.Map({
        container: mapRef.current,
        style: {
          version: 8,
          sources: { amap: { type: "raster",
            tiles: [1,2,3,4].map(i => `https://webst0${i}.is.autonavi.com/appmaptile?style=7&x={x}&y={y}&z={z}`),
            tileSize: 256, attribution: "© 高德地图" } },
          layers: [{ id: "amap", type: "raster", source: "amap" }],
        },
        center: [116.4, 34], zoom: 4,
      });
      map.on("load", () => {
        map.fitBounds([Math.min(...lngs)-1, Math.min(...lats)-1, Math.max(...lngs)+1, Math.max(...lats)+1], { padding: 60, duration: 0 });
        // 出发地→目的地连线
        points.forEach((p, i) => {
          const oName = p.origin || "";
          const oc = CITY_COORDS[oName];
          if (oc && oName !== p.destination) {
            const og = wgs2gcj(oc[0], oc[1]);
            map.addSource(`line-${i}`, { type: "geojson", data: {
              type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [og, p.gcj] } } });
            map.addLayer({ id: `line-${i}`, type: "line", source: `line-${i}`,
              paint: { "line-color": "#3d94d1", "line-width": 2, "line-opacity": .55, "line-dasharray": [2, 1.5] } });
          }
        });
        // 目的地标记（大小按到访次数）
        points.forEach(p => {
          const count = destCount[p.destination!];
          const size = Math.min(14 + count * 4, 30);
          const el = document.createElement("div");
          el.style.cssText = `width:${size}px;height:${size}px;border-radius:50%;
            background:#1e5f8f;border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,.4);
            display:grid;place-items:center;color:#fff;font-size:11px;font-weight:700;
            font-family:-apple-system,'PingFang SC',sans-serif;cursor:pointer;`;
          el.textContent = String(count);
          el.title = `${p.destination}（${count} 次）`;
          new ml.Marker({ element: el }).setLngLat(p.gcj).addTo(map);
        });
      });
    });
    return () => { cancelled = true; };
  }, [trips]);

  if (trips === null) return <div className={styles.loading}>加载差旅历史…</div>;
  if (!geoTrips.length) {
    return (
      <div className={styles.empty}>
        <h2>暂无差旅历史</h2>
        <p>规划过行程后（或已有历史记录的目的地），这里会显示足迹地图与统计。</p>
        <a href="/">← 返回对话</a>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.brand}><span className={styles.logo}>📍</span>
          <div><h1>差旅足迹</h1><small>{userId} · {geoTrips.length} 条行程</small></div>
        </div>
        <a className={styles.back} href="/">← 返回对话</a>
      </header>
      <main className={styles.main}>
        <div className={styles.map} ref={mapRef} />
        <aside className={styles.side}>
          <h3>常去目的地</h3>
          <div className={styles.ranking}>
            {ranking.map(([city, count], i) => {
              const pct = count / ranking[0][1] * 100;
              return (
                <div key={city} className={styles.rankRow}>
                  <span className={styles.rankNo}>{i + 1}</span>
                  <span className={styles.rankCity}>{city}</span>
                  <div className={styles.rankTrack}><div className={styles.rankBar} style={{ width: `${pct}%` }} /></div>
                  <span className={styles.rankCount}>{count} 次</span>
                </div>
              );
            })}
          </div>
          <h3>行程时间线</h3>
          <div className={styles.timeline}>
            {(trips || []).slice(-12).reverse().map((t, i) => (
              <div key={i} className={styles.tlItem}>
                <span className={styles.tlDate}>{(t.start_date || "").slice(0, 10)}</span>
                <span className={styles.tlRoute}>{t.origin || "?"} → {t.destination || "?"}</span>
                <span className={styles.tlPurpose}>{t.purpose || ""}</span>
              </div>
            ))}
          </div>
        </aside>
      </main>
    </div>
  );
}
