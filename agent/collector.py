"""
PulseOps - agent/collector.py
==============================
Runs ON the server being monitored.
Collects OS metrics every N seconds and sends them to the backend.

CS 415 (OS Principles):
  On Linux, /proc is a virtual filesystem maintained by the kernel.
  Every metric here (CPU, memory, processes) comes from real kernel
  data structures. psutil is a cross-platform wrapper around those files.

  Try these in a Linux terminal to see raw data:
    cat /proc/stat         -> CPU tick counters
    cat /proc/meminfo      -> memory in kilobytes
    cat /proc/[pid]/status -> a specific process
"""

import time
import json
import httpx
import psutil
from datetime import datetime, timezone


class MetricsCollector:
    """
    CS 413 (Adv. Software Eng.) - Why a class?
      State must persist between collections:
        - server_id, api_url  : config, set once
        - _prev_net           : previous network counters (needed for rate calc)
        - http                : reused TCP connection (cheaper than new conn each time)
    """

    def __init__(self, server_id: str, api_url: str, api_key: str, interval_seconds: int = 5):
        self.server_id = server_id
        self.api_url   = api_url
        self.api_key   = api_key
        self.interval  = interval_seconds
        self.http      = httpx.Client(
            timeout=30.0,
            headers={"Authorization": f"Bearer {api_key}"}
        )
        self._prev_net      = None
        self._prev_net_time = None

    def collect_cpu(self) -> dict:
        """
        CS 415 - CPU Scheduling:
          The OS counts CPU time in "ticks". cpu_percent computes:
            (non-idle ticks / total ticks) x 100 over a 1-second window.
          percpu=True reveals per-core load.
          A single core at 100% while others are idle = single-threaded bottleneck.
        """
        return {
            "percent_total":    psutil.cpu_percent(interval=1),
            "percent_per_core": psutil.cpu_percent(interval=None, percpu=True),
            "logical_cores":    psutil.cpu_count(logical=True),
            "physical_cores":   psutil.cpu_count(logical=False),
            "frequency_mhz":    psutil.cpu_freq().current if psutil.cpu_freq() else None,
        }

    def collect_memory(self) -> dict:
        """
        CS 415 - Memory Management:
          RAM is divided into 4KB pages. The OS tracks:
            free     : completely unused pages
            cached   : pages holding file data (can be reclaimed instantly)
            available: free + reclaimable cache <- the number that actually matters
          Swap = disk space used as overflow RAM.
          High swap = OS moving pages between RAM and disk = serious slowdown.
        """
        ram  = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "ram": {
                "total_bytes":     ram.total,
                "used_bytes":      ram.used,
                "available_bytes": ram.available,
                "percent_used":    ram.percent,
                "cached_bytes":    getattr(ram, "cached", 0),
                "buffers_bytes":   getattr(ram, "buffers", 0),
            },
            "swap": {
                "total_bytes":  swap.total,
                "used_bytes":   swap.used,
                "percent_used": swap.percent,
            },
        }

    def collect_disk(self) -> dict:
        """
        CS 415 - File Systems & I/O:
          io_counters are cumulative since boot - they only increase.
          We store raw values and compute rates on the backend
          by comparing two snapshots separated by time.
        """
        partitions = []
        for p in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(p.mountpoint)
                partitions.append({
                    "device":       p.device,
                    "mountpoint":   p.mountpoint,
                    "fstype":       p.fstype,
                    "total_bytes":  usage.total,
                    "used_bytes":   usage.used,
                    "free_bytes":   usage.free,
                    "percent_used": usage.percent,
                })
            except PermissionError:
                continue
        io = psutil.disk_io_counters()
        return {
            "partitions":  partitions,
            "io_counters": io._asdict() if io else {},
        }

    def collect_network(self) -> dict:
        """
        Why time.monotonic() instead of time.time()?
          time.time() can jump backward during NTP syncs.
          time.monotonic() only ever moves forward.
          Always use monotonic() when measuring durations.
        """
        current = psutil.net_io_counters()
        now     = time.monotonic()
        sent_rate = recv_rate = None
        if self._prev_net and self._prev_net_time:
            elapsed = now - self._prev_net_time
            if elapsed > 0:
                sent_rate = (current.bytes_sent - self._prev_net.bytes_sent) / elapsed
                recv_rate = (current.bytes_recv - self._prev_net.bytes_recv) / elapsed
        self._prev_net      = current
        self._prev_net_time = now
        return {
            "bytes_sent_total":   current.bytes_sent,
            "bytes_recv_total":   current.bytes_recv,
            "packets_sent_total": current.packets_sent,
            "packets_recv_total": current.packets_recv,
            "errors_in":          current.errin,
            "errors_out":         current.errout,
            "bytes_sent_per_sec": sent_rate,
            "bytes_recv_per_sec": recv_rate,
        }

    def collect_top_processes(self, top_n: int = 10) -> list:
        """
        CS 415 - Process Management:
          Every program is a process: isolated memory, file handles, CPU time.
          process_iter() walks the kernel process table (like ps aux).
          Statuses: running, sleeping (normal), zombie, disk-sleep (bad).
        """
        procs = []
        for p in psutil.process_iter(["pid", "name", "status", "cpu_percent", "memory_percent", "num_threads"]):
            try:
                procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return sorted(procs, key=lambda p: p["cpu_percent"] or 0, reverse=True)[:top_n]

    def collect_snapshot(self) -> dict:
        """One complete picture of the server. Always UTC timestamps."""
        return {
            "server_id":     self.server_id,
            "timestamp":     datetime.now(timezone.utc).isoformat(),
            "cpu":           self.collect_cpu(),
            "memory":        self.collect_memory(),
            "disk":          self.collect_disk(),
            "network":       self.collect_network(),
            "top_processes": self.collect_top_processes(),
        }

    def send_snapshot(self, snapshot: dict) -> bool:
        """
        CS 413 - Resilience: never raise here.
          A failed send must not crash the agent.
          Log, return False, keep the loop running.
        """
        try:
            r = self.http.post(f"{self.api_url}/api/metrics", json=snapshot)
            r.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            print(f"[Agent] Backend error {e.response.status_code}: {e.response.text}")
        except httpx.RequestError as e:
            print(f"[Agent] Cannot reach backend: {e}")
        return False

    def run(self, dry_run: bool = False):
        """
        CS 415 - Daemon loop.
          Ctrl+C -> SIGINT -> KeyboardInterrupt -> clean shutdown.
          dry_run=True prints to console, no backend needed.
        """
        print("[PulseOps Agent]")
        print(f"  Server ID : {self.server_id}")
        print(f"  Backend   : {self.api_url}")
        print(f"  Interval  : {self.interval}s")
        print(f"  Mode      : {'DRY RUN' if dry_run else 'LIVE'}")
        print("-" * 50)
        while True:
            try:
                snapshot = self.collect_snapshot()
                if dry_run:
                    print(json.dumps(snapshot, indent=2, default=str))
                    print("-" * 50)
                else:
                    ok = self.send_snapshot(snapshot)
                    print(f"[{snapshot['timestamp']}] {'ok' if ok else 'FAILED'}")
                time.sleep(self.interval)
            except KeyboardInterrupt:
                print("\n[Agent] Shutting down cleanly.")
                self.http.close()
                break
            except Exception as e:
                print(f"[Agent] Unexpected error: {e}")
                time.sleep(self.interval)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    collector = MetricsCollector(
        server_id        = os.getenv("SERVER_ID", "my-server-01"),
        api_url          = os.getenv("API_URL", "http://localhost:8000"),
        api_key          = os.getenv("AGENT_API_KEY", ""),
        interval_seconds = int(os.getenv("AGENT_INTERVAL", "5")),
    )
    collector.run(dry_run=False)
