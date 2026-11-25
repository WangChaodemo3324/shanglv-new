"use client";
import { useMemo } from "react";

interface Activity {
  time: string;
  location: string;
  description?: string;
  transport?: string;
  lat?: number | null;
  lng?: number | null;
}

interface DayPlan {
  day: number;
  date?: string;
  city?: string;
  theme?: string;
  activities: Activity[];
  meals?: { lunch?: string; dinner?: string };
}

export interface Itinerary {
  title?: string;
  duration?: string;
  route?: string;
  daily_plans?: DayPlan[];
  accommodation?: string;
  tips?: string[];
}

/** 行程可视化时间轴：每天一条轴线，活动为节点，交通方式串联 */
export default function TripTimeline({ itinerary }: { itinerary: Itinerary }) {
  const days = itinerary.daily_plans || [];
  const icon = useMemo(() => pickIcon(itinerary.title || ""), [itinerary.title]);

  if (!days.length) return null;

  return (
    <div className="trip-timeline">
      <div className="tt-header">
        <span className="tt-icon">{icon}</span>
        <div className="tt-title">
          <b>{itinerary.title || "行程规划"}</b>
          <small>{itinerary.duration} · {itinerary.route}</small>
        </div>
      </div>

      {days.map((d) => (
        <div key={d.day} className="tt-day">
          <div className="tt-day-head">
            <span className="tt-day-badge">D{d.day}</span>
            <div className="tt-day-meta">
              <b>{d.date}{d.city ? ` · ${d.city}` : ""}</b>
              {d.theme && <small>{d.theme}</small>}
            </div>
          </div>

          <div className="tt-line">
            {d.activities.map((a, i) => (
              <div key={i} className="tt-node">
                <span className="tt-dot" />
                <div className="tt-card">
                  <div className="tt-time">{a.time}</div>
                  <b className="tt-loc">{a.location}</b>
                  {a.description && <p className="tt-desc">{a.description}</p>}
                </div>
                {a.transport && i < d.activities.length - 1 && (
                  <div className="tt-transport">↓ {a.transport}</div>
                )}
              </div>
            ))}
          </div>

          {d.meals && (d.meals.lunch || d.meals.dinner) && (
            <div className="tt-meals">
              {d.meals.lunch && <span>🍜 {d.meals.lunch}</span>}
              {d.meals.dinner && <span>🍽️ {d.meals.dinner}</span>}
            </div>
          )}
        </div>
      ))}

      {itinerary.accommodation && (
        <div className="tt-hotel">🏨 {itinerary.accommodation}</div>
      )}
      {itinerary.tips?.length ? (
        <div className="tt-tips">
          {itinerary.tips.slice(0, 3).map((t, i) => <span key={i}>💡 {t}</span>)}
        </div>
      ) : null}
    </div>
  );
}

function pickIcon(title: string): string {
  if (/出差|商务/.test(title)) return "💼";
  if (/游|玩|度假/.test(title)) return "🏝️";
  return "✈️";
}
