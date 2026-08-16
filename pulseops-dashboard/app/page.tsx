"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import {
  LineChart, Line, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine
} from "recharts";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Server {
  server_id: string;
  latest_timestamp: string;
  latest_cpu_percent: number;
  latest_memory_percent: number;
}

interface Snapshot {
  timestamp: string;
  cpu_percent: number;
  memory_percent: number;
}

interface MetricDetail {
  current: number;
  baseline_mean: number;
  baseline_stdev: number;
  z_score: number | null;
  status: string;
}

interface Anomaly {
  overall: string;
  is_anomaly: boolean;
  timestamp: string;
  metrics: { cpu: MetricDetail; memory: MetricDetail };
  baseline_sample_count: number;
}

interface Process {
  pid: number;
  name: string;
  cpu_percent: number;
  memory_percent: number;
  status: string;
}

interface LatestSnapshot {
  timestamp: string;
  cpu_percent: number;
  memory_percent: number;
  cpu: { logical_cores: number; physical_cores: number; frequency_mhz: number };
  memory: { ram: { total_bytes: number; available_bytes: number } };
  processes: Process[];
}

// ── Constants ─────────────────────────────────────────────────────────────────

const API           = "http://localhost:8000";
const POLL_MS       = 5000;
const HISTORY_LIMIT = 60;

// ── Utils ─────────────────────────────────────────────────────────────────────

const fmtTime  = (ts: string) => new Date(ts).toLocaleTimeString("en-US", { hour12: false });
const fmtBytes = (b: number)  => b > 1e9 ? `${(b/1e9).toFixed(1)}G` : `${(b/1e6).toFixed(0)}M`;
const fmtPct   = (n: number)  => n?.toFixed(1) ?? "—";

function statusColor(s: string) {
  if (s === "severe") return "#ef4444";
  if (s === "mild")   return "#f59e0b";
  return "#10b981";
}

function statusLabel(s: string) {
  if (s === "severe") return "CRIT";
  if (s === "mild")   return "WARN";
  if (s === "normal") return "OK";
  return "—";
}

const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

async function apiFetch<T>(path: string): Promise<T | null> {
  try {
    const separator = path.includes("?") ? "&" : "?";
    const r = await fetch(`${API}${path}${separator}t=${Date.now()}`, {
      cache: "no-store",
      headers: {
        "Authorization": `Bearer ${API_KEY}`,
      },
    });
    return r.ok ? r.json() : null;
  } catch { return null; }
}

// ── Sparkline ─────────────────────────────────────────────────────────────────

function Spark({ data, dataKey, color, mean }: {
  data: Snapshot[]; dataKey: keyof Snapshot; color: string; mean?: number;
}) {
  if (!data.length) return (
    <div style={{ height: 56, display: "flex", alignItems: "center" }}>
      <span className="mono" style={{ color: "var(--text-muted)", fontSize: 10 }}>
        collecting data...
      </span>
    </div>
  );

  return (
    <ResponsiveContainer width="100%" height={72}>
      <LineChart data={data} margin={{ top: 4, right: 4, bottom: 4, left: 0 }}>
        <YAxis domain={['auto', 'auto']} hide />
        {mean !== undefined && (
          <ReferenceLine y={mean} stroke={color} strokeDasharray="2 4" strokeOpacity={0.35} />
        )}
        <Line
          type="monotone"
          dataKey={dataKey as string}
          stroke={color}
          dot={false}
          strokeWidth={1.5}
          isAnimationActive={false}
        />
        <Tooltip
          contentStyle={{
            background: "#0f1520",
            border: "1px solid #1c2a3a",
            fontSize: 11,
            fontFamily: "JetBrains Mono, monospace",
            padding: "4px 8px",
          }}
          labelFormatter={(l: unknown) => fmtTime(l as string)}
          formatter={(v: unknown) => [`${(v as number).toFixed(1)}%`]}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ── Status Strip ──────────────────────────────────────────────────────────────

function StatusStrip({ servers, anomalies }: {
  servers: Server[];
  anomalies: Record<string, Anomaly>;
}) {
  return (
    <div style={{ borderBottom: "1px solid var(--border)" }}>
      <div style={{
        display: "grid",
        gridTemplateColumns: "180px 80px 80px 80px 80px 1fr 140px",
        padding: "4px 24px",
        background: "var(--bg-raised)",
        borderBottom: "1px solid var(--border)",
        color: "var(--text-muted)",
        fontFamily: "JetBrains Mono, monospace",
        fontSize: 10,
        letterSpacing: "0.08em",
        textTransform: "uppercase",
      }}>
        <span>Server</span>
        <span>Status</span>
        <span>CPU</span>
        <span>Mem</span>
        <span>z(cpu)</span>
        <span>z(mem)</span>
        <span style={{ textAlign: "right" }}>Last seen</span>
      </div>
      {servers.map(s => {
        const a   = anomalies[s.server_id];
        const overall = a?.overall ?? "—";
        const color   = statusColor(overall);
        return (
          <div key={s.server_id} style={{
            display: "grid",
            gridTemplateColumns: "180px 80px 80px 80px 80px 1fr 140px",
            padding: "6px 24px",
            borderBottom: "1px solid var(--border)",
            fontFamily: "JetBrains Mono, monospace",
            fontSize: 12,
            alignItems: "center",
          }}>
            <span style={{ color: "var(--text-primary)", display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: color, display: "inline-block" }} />
              {s.server_id}
            </span>
            <span style={{ color, fontWeight: 600, fontSize: 11 }}>{statusLabel(overall)}</span>
            <span style={{ color: "var(--text-primary)" }}>{fmtPct(s.latest_cpu_percent)}%</span>
            <span style={{ color: "var(--text-primary)" }}>{fmtPct(s.latest_memory_percent)}%</span>
            <span style={{ color: statusColor(a?.metrics?.cpu?.status ?? "normal") }}>
              {a?.metrics?.cpu?.z_score != null ? a.metrics.cpu.z_score.toFixed(2) : "—"}
            </span>
            <span style={{ color: statusColor(a?.metrics?.memory?.status ?? "normal") }}>
              {a?.metrics?.memory?.z_score != null ? a.metrics.memory.z_score.toFixed(2) : "—"}
            </span>
            <span style={{ color: "var(--text-dim)", textAlign: "right", fontSize: 11 }}>
              {s.latest_timestamp ? fmtTime(s.latest_timestamp) : "—"}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ── Server Detail Panel ───────────────────────────────────────────────────────

function ServerPanel({ server }: { server: Server }) {
  const [history,  setHistory]  = useState<Snapshot[]>([]);
  const [anomaly,  setAnomaly]  = useState<Anomaly | null>(null);
  const [latest,   setLatest]   = useState<LatestSnapshot | null>(null);

  const fetchAll = useCallback(async () => {
    const [h, a, l] = await Promise.all([
      apiFetch<{ snapshots: Snapshot[] }>(`/api/metrics/${server.server_id}?limit=${HISTORY_LIMIT}`),
      apiFetch<Anomaly>(`/api/anomalies/${server.server_id}/latest`),
      apiFetch<LatestSnapshot>(`/api/metrics/${server.server_id}/latest`),
    ]);
    if (h) {
      console.log("history data:", h);
      setHistory(h.snapshots.slice().reverse());
    }
    if (a) setAnomaly({ ...a });
    if (l) setLatest({ ...l });
  }, [server.server_id]);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, POLL_MS);
    return () => clearInterval(id);
  }, [fetchAll]);

  const cpu = anomaly?.metrics?.cpu;
  const mem = anomaly?.metrics?.memory;
  const overall = anomaly?.overall ?? "normal";
  const borderColor = statusColor(overall);

  return (
    <div style={{
      background: "var(--bg-surface)",
      border: `1px solid ${anomaly?.is_anomaly ? borderColor + "60" : "var(--border)"}`,
      borderTop: `2px solid ${borderColor}`,
    }}>
      {/* Panel header */}
      <div style={{
        padding: "10px 16px",
        borderBottom: "1px solid var(--border)",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        background: "var(--bg-raised)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{
            width: 8, height: 8, borderRadius: "50%",
            background: borderColor,
            boxShadow: anomaly?.is_anomaly ? `0 0 6px ${borderColor}` : "none",
          }} />
          <span className="mono" style={{ color: "var(--text-primary)", fontSize: 13, fontWeight: 500 }}>
            {server.server_id}
          </span>
          {latest && (
            <span className="mono" style={{ color: "var(--text-muted)", fontSize: 10 }}>
              {latest.cpu?.logical_cores}c / {fmtBytes(latest.memory?.ram?.total_bytes ?? 0)} RAM
            </span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          {anomaly?.is_anomaly && (
            <span className="mono" style={{
              color: borderColor, fontSize: 11, fontWeight: 700,
              letterSpacing: "0.1em",
              padding: "2px 8px",
              border: `1px solid ${borderColor}40`,
              background: `${borderColor}10`,
            }}>
              ▲ {overall.toUpperCase()}
            </span>
          )}
          <span className="mono" style={{ color: "var(--text-muted)", fontSize: 10 }}>
            {anomaly?.baseline_sample_count ?? 0} samples
          </span>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "260px 1fr", gap: 0 }}>
        {/* Left — metrics */}
        <div style={{ padding: "12px 16px", borderRight: "1px solid var(--border)" }}>

          {/* CPU */}
          <div style={{ marginBottom: 10 }}>
            <div style={{
              display: "flex", justifyContent: "space-between", alignItems: "baseline",
              marginBottom: 4,
            }}>
              <span style={{ color: "var(--text-dim)", fontSize: 10, letterSpacing: "0.08em", textTransform: "uppercase" }}>
                CPU
              </span>
              <span className="mono" style={{
                fontSize: 20, fontWeight: 700,
                color: statusColor(cpu?.status ?? "normal"),
              }}>
                {fmtPct(server.latest_cpu_percent)}%
              </span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span className="mono" style={{ color: "var(--text-muted)", fontSize: 10 }}>
                μ={cpu ? fmtPct(cpu.baseline_mean) : "—"}%
              </span>
              <span className="mono" style={{ color: statusColor(cpu?.status ?? "normal"), fontSize: 10 }}>
                z={cpu?.z_score != null ? cpu.z_score.toFixed(2) : "—"}
              </span>
            </div>
          </div>

          {/* Memory */}
          <div style={{ marginBottom: 10 }}>
            <div style={{
              display: "flex", justifyContent: "space-between", alignItems: "baseline",
              marginBottom: 4,
            }}>
              <span style={{ color: "var(--text-dim)", fontSize: 10, letterSpacing: "0.08em", textTransform: "uppercase" }}>
                Memory
              </span>
              <span className="mono" style={{
                fontSize: 20, fontWeight: 700,
                color: statusColor(mem?.status ?? "normal"),
              }}>
                {fmtPct(server.latest_memory_percent)}%
              </span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span className="mono" style={{ color: "var(--text-muted)", fontSize: 10 }}>
                μ={mem ? fmtPct(mem.baseline_mean) : "—"}%
              </span>
              <span className="mono" style={{ color: statusColor(mem?.status ?? "normal"), fontSize: 10 }}>
                z={mem?.z_score != null ? mem.z_score.toFixed(2) : "—"}
              </span>
            </div>
          </div>

          {/* Divider */}
          <div style={{ borderTop: "1px solid var(--border)", marginBottom: 8 }} />

          {/* Process table */}
          <div style={{ color: "var(--text-dim)", fontSize: 10, letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            Top Processes
          </div>
          {(latest?.processes ?? []).slice(0, 5).map((p, i) => (
            <div key={i} style={{
              display: "grid", gridTemplateColumns: "1fr 36px 36px",
              padding: "2px 0", borderBottom: "1px solid var(--border)",
              alignItems: "center",
            }}>
              <span className="mono" style={{
                color: "var(--text-primary)", fontSize: 10,
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}>
                {p.name}
              </span>
              <span className="mono" style={{ color: "#0ea5e9", fontSize: 10, textAlign: "right" }}>
                {fmtPct(p.cpu_percent)}%
              </span>
              <span className="mono" style={{ color: "#8b5cf6", fontSize: 10, textAlign: "right" }}>
                {fmtPct(p.memory_percent)}%
              </span>
            </div>
          ))}
        </div>

        {/* Right — charts */}
        <div style={{ padding: "12px 16px", display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <div style={{
              display: "flex", justifyContent: "space-between",
              marginBottom: 4,
            }}>
              <span style={{ color: "var(--text-dim)", fontSize: 10, letterSpacing: "0.08em", textTransform: "uppercase" }}>
                CPU %
              </span>
              <span className="mono" style={{ color: "var(--text-muted)", fontSize: 10 }}>
                last {HISTORY_LIMIT} readings
              </span>
            </div>
            <Spark data={history} dataKey="cpu_percent" color="#0ea5e9" mean={cpu?.baseline_mean} />
          </div>

          <div style={{ borderTop: "1px solid var(--border)", paddingTop: 12 }}>
            <div style={{ marginBottom: 4 }}>
              <span style={{ color: "var(--text-dim)", fontSize: 10, letterSpacing: "0.08em", textTransform: "uppercase" }}>
                Memory %
              </span>
            </div>
            <Spark data={history} dataKey="memory_percent" color="#8b5cf6" mean={mem?.baseline_mean} />
          </div>

          <div style={{ borderTop: "1px solid var(--border)", paddingTop: 8 }}>
            <span className="mono" style={{ color: "var(--text-muted)", fontSize: 10 }}>
              last update {latest ? fmtTime(latest.timestamp) : "—"}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Main Dashboard ────────────────────────────────────────────────────────────

export default function Dashboard() {
  const [servers,   setServers]   = useState<Server[]>([]);
  const [anomalies, setAnomalies] = useState<Record<string, Anomaly>>({});
  const [pulse,     setPulse]     = useState(false);
  const [clock,     setClock]     = useState("");
  const tickRef = useRef(0);

  const fetchServers = useCallback(async () => {
    const data = await apiFetch<Server[]>("/api/servers");
    if (!data) return;
    setServers(data);

    // Fetch anomaly for each server
    const entries = await Promise.all(
      data.map(async s => {
        const a = await apiFetch<Anomaly>(`/api/anomalies/${s.server_id}/latest`);
        return [s.server_id, a] as [string, Anomaly | null];
      })
    );
    const map: Record<string, Anomaly> = {};
    entries.forEach(([id, a]) => { if (a) map[id] = a; });
    setAnomalies(map);

    // Pulse the live indicator
    setPulse(true);
    setTimeout(() => setPulse(false), 300);
    tickRef.current += 1;
  }, []);

  useEffect(() => {
    setClock(new Date().toLocaleTimeString("en-US", { hour12: false }));
    fetchServers();
    const pollId  = setInterval(fetchServers, POLL_MS);
    const clockId = setInterval(() => setClock(new Date().toLocaleTimeString("en-US", { hour12: false })), 1000);
    return () => { clearInterval(pollId); clearInterval(clockId); };
  }, [fetchServers]);

  const critCount = Object.values(anomalies).filter(a => a.overall === "severe").length;
  const warnCount = Object.values(anomalies).filter(a => a.overall === "mild").length;

  return (
    <div style={{ minHeight: "100vh", background: "var(--bg-base)", display: "flex", flexDirection: "column" }}>

      {/* Topbar */}
      <header style={{
        height: 44,
        background: "var(--bg-raised)",
        borderBottom: "1px solid var(--border)",
        display: "flex",
        alignItems: "center",
        padding: "0 24px",
        gap: 24,
        flexShrink: 0,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span className="mono" style={{
            color: "#0ea5e9", fontWeight: 700, fontSize: 13,
            letterSpacing: "0.15em",
          }}>
            PULSEOPS
          </span>
          <span style={{
            width: 6, height: 6, borderRadius: "50%",
            background: pulse ? "#10b981" : "#1c4a3a",
            transition: "background 0.15s",
          }} />
          <span className="mono" style={{ color: "var(--text-muted)", fontSize: 10 }}>LIVE</span>
        </div>

        <div style={{ width: 1, height: 20, background: "var(--border)" }} />

        <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
          <span style={{ color: "var(--text-dim)", fontSize: 12 }}>
            {servers.length} server{servers.length !== 1 ? "s" : ""}
          </span>
          {critCount > 0 && (
            <span className="mono" style={{
              color: "#ef4444", fontSize: 11, fontWeight: 600,
              padding: "1px 6px", border: "1px solid #ef444440",
              background: "#ef444410",
            }}>
              {critCount} CRIT
            </span>
          )}
          {warnCount > 0 && (
            <span className="mono" style={{
              color: "#f59e0b", fontSize: 11, fontWeight: 600,
              padding: "1px 6px", border: "1px solid #f59e0b40",
              background: "#f59e0b10",
            }}>
              {warnCount} WARN
            </span>
          )}
        </div>

        <div style={{ marginLeft: "auto", display: "flex", gap: 20, alignItems: "center" }}>
          <span className="mono" style={{ color: "var(--text-dim)", fontSize: 11 }}>
            poll {POLL_MS / 1000}s
          </span>
          <span className="mono" style={{ color: "var(--text-primary)", fontSize: 12 }}>{clock}</span>
        </div>
      </header>

      {/* Status strip */}
      {servers.length > 0 && (
        <StatusStrip servers={servers} anomalies={anomalies} />
      )}

      {/* Server panels */}
      <main style={{ padding: "16px 24px", flex: 1 }}>
        {servers.length === 0 ? (
          <div style={{
            display: "flex", flexDirection: "column",
            alignItems: "center", justifyContent: "center",
            height: 200, gap: 8,
          }}>
            <span className="mono" style={{ color: "var(--text-muted)", fontSize: 13 }}>
              no servers reporting
            </span>
            <span className="mono" style={{ color: "var(--text-muted)", fontSize: 11 }}>
              python agent/collector.py
            </span>
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
            {servers.map(s => <ServerPanel key={s.server_id} server={s} />)}
          </div>
        )}
      </main>

      {/* Footer */}
      <footer style={{
        height: 28,
        borderTop: "1px solid var(--border)",
        display: "flex",
        alignItems: "center",
        padding: "0 24px",
        justifyContent: "space-between",
      }}>
        <span className="mono" style={{ color: "var(--text-muted)", fontSize: 10 }}>
          PulseOps v0.7.0
        </span>
        <span className="mono" style={{ color: "var(--text-muted)", fontSize: 10 }}>
          tick #{tickRef.current}
        </span>
      </footer>
    </div>
  );
}