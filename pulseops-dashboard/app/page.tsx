"use client";

import { useEffect, useState, useCallback } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer
} from "recharts";

// ── Types ────────────────────────────────────────────────────────────────────

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

interface AnomalyMetric {
  current: number;
  baseline_mean: number;
  z_score: number;
  status: string;
}

interface Anomaly {
  overall: string;
  is_anomaly: boolean;
  metrics: { cpu: AnomalyMetric; memory: AnomalyMetric };
}

// ── Constants ─────────────────────────────────────────────────────────────────

const API = "http://localhost:8000";
const POLL_INTERVAL = 5000;

// ── Helpers ───────────────────────────────────────────────────────────────────

function severityColor(status: string) {
  if (status === "severe") return "text-red-400";
  if (status === "mild")   return "text-yellow-400";
  return "text-cyan-400";
}

function severityBorder(overall: string) {
  if (overall === "severe") return "border-red-500";
  if (overall === "mild")   return "border-yellow-500";
  return "border-cyan-800";
}

function fmt(ts: string) {
  return new Date(ts).toLocaleTimeString();
}

// ── Sub-components ────────────────────────────────────────────────────────────

function MetricChart({ data, dataKey, color, label }: {
  data: Snapshot[]; dataKey: keyof Snapshot; color: string; label: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-mono text-slate-400 uppercase tracking-widest">{label}</span>
      <ResponsiveContainer width="100%" height={80}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="timestamp" tickFormatter={fmt} tick={{ fontSize: 9, fill: "#64748b" }} />
          <YAxis domain={[0, 100]} tick={{ fontSize: 9, fill: "#64748b" }} width={28} />
          <Tooltip
            contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 11 }}
            labelFormatter={(label: unknown) => fmt(label as string)}
            formatter={(value: unknown) => [`${(value as number).toFixed(1)}%`, label]}
          />
          <Line type="monotone" dataKey={dataKey} stroke={color}
            dot={false} strokeWidth={1.5} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function AnomalyBadge({ anomaly }: { anomaly: Anomaly | null }) {
  if (!anomaly) return <span className="text-xs font-mono text-slate-600">loading...</span>;
  if (!anomaly.is_anomaly) return <span className="text-xs font-mono text-cyan-500">● normal</span>;
  const color = anomaly.overall === "severe" ? "text-red-400" : "text-yellow-400";
  return <span className={`text-xs font-mono font-bold ${color} animate-pulse`}>▲ {anomaly.overall}</span>;
}

function StatRow({ label, value, unit, z, status }: {
  label: string; value: number; unit: string; z: number | null; status: string;
}) {
  return (
    <div className="flex items-center justify-between py-1 border-b border-slate-800">
      <span className="text-xs font-mono text-slate-500 w-16">{label}</span>
      <span className="text-sm font-mono text-white tabular-nums">{value.toFixed(1)}{unit}</span>
      {z !== null && (
        <span className={`text-xs font-mono ${severityColor(status)}`}>
          z={z.toFixed(2)} {status}
        </span>
      )}
    </div>
  );
}

// ── Server Card ───────────────────────────────────────────────────────────────

function ServerCard({ server }: { server: Server }) {
  const [history, setHistory]   = useState<Snapshot[]>([]);
  const [anomaly, setAnomaly]   = useState<Anomaly | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const [metricsRes, anomalyRes] = await Promise.all([
        fetch(`${API}/api/metrics/${server.server_id}?limit=40&t=${Date.now()}`, { cache: "no-store" }),
        fetch(`${API}/api/anomalies/${server.server_id}/latest?t=${Date.now()}`, { cache: "no-store" }),
      ]);
      if (metricsRes.ok) {
        const data = await metricsRes.json();
        // Reverse so oldest is left on chart
        setHistory(data.snapshots.slice().reverse());
      }
      if (anomalyRes.ok) setAnomaly({ ...await anomalyRes.json() });
    } catch { /* backend may be starting */ }
  }, [server.server_id]);

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, POLL_INTERVAL);
    return () => clearInterval(id);
  }, [fetchData]);

  const cpu    = anomaly?.metrics?.cpu;
  const memory = anomaly?.metrics?.memory;

  return (
    <div className={`border ${severityBorder(anomaly?.overall ?? "")} bg-slate-900 p-4 flex flex-col gap-4`}>

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse" />
          <span className="font-mono text-sm text-white">{server.server_id}</span>
        </div>
        <AnomalyBadge anomaly={anomaly} />
      </div>

      {/* Stats */}
      <div>
        <StatRow
          label="CPU"
          value={server.latest_cpu_percent ?? 0}
          unit="%"
          z={cpu?.z_score ?? null}
          status={cpu?.status ?? "normal"}
        />
        <StatRow
          label="MEM"
          value={server.latest_memory_percent ?? 0}
          unit="%"
          z={memory?.z_score ?? null}
          status={memory?.status ?? "normal"}
        />
        {cpu && (
          <div className="mt-1 flex justify-between text-xs font-mono text-slate-600">
            <span>baseline cpu {cpu.baseline_mean.toFixed(1)}%</span>
            <span>baseline mem {memory?.baseline_mean.toFixed(1)}%</span>
          </div>
        )}
      </div>

      {/* Charts */}
      <div className="flex flex-col gap-3">
        <MetricChart data={history} dataKey="cpu_percent"    color="#22d3ee" label="CPU %" />
        <MetricChart data={history} dataKey="memory_percent" color="#a78bfa" label="Memory %" />
      </div>

      {/* Timestamp */}
      <div className="text-xs font-mono text-slate-600 text-right">
        last update {fmt(server.latest_timestamp)}
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function Dashboard() {
  const [servers, setServers] = useState<Server[]>([]);
  const [tick, setTick]       = useState(0);
  const [now, setNow]         = useState("");

  useEffect(() => {
    setNow(new Date().toLocaleTimeString());
    const fetchServers = async () => {
      try {
        const res = await fetch(`${API}/api/servers?t=${Date.now()}`, { cache: "no-store" });
        if (res.ok) setServers(await res.json());
      } catch { /* backend starting */ }
    };
    fetchServers();
    const id = setInterval(() => {
      fetchServers();
      setTick(t => t + 1);
      setNow(new Date().toLocaleTimeString());
    }, POLL_INTERVAL);
    return () => clearInterval(id);
  }, []);

  return (
    <main className="min-h-screen bg-slate-950 text-white">

      {/* Top bar */}
      <header className="border-b border-slate-800 px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="font-mono text-cyan-400 font-bold tracking-widest text-sm">PULSEOPS</span>
          <span className="text-slate-600 text-xs font-mono">/ dashboard</span>
        </div>
        <div className="flex items-center gap-4 text-xs font-mono text-slate-500">
          <span>{servers.length} server{servers.length !== 1 ? "s" : ""} monitored</span>
          <span className="text-slate-700">|</span>
          <span>poll {POLL_INTERVAL / 1000}s</span>
          <span className="text-slate-700">|</span>
          <span className="text-cyan-600">{now}</span>
        </div>
      </header>

      {/* Grid */}
      <div className="p-6">
        {servers.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-64 gap-3">
            <span className="font-mono text-slate-600 text-sm">no servers reporting</span>
            <span className="font-mono text-slate-700 text-xs">
              start the agent: python agent/collector.py
            </span>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {servers.map(s => <ServerCard key={s.server_id} server={s} />)}
          </div>
        )}
      </div>

      {/* Footer */}
      <footer className="border-t border-slate-800 px-6 py-2 text-xs font-mono text-slate-700 flex justify-between">
        <span>PulseOps v0.6.0</span>
        <span>tick #{tick}</span>
      </footer>
    </main>
  );
}