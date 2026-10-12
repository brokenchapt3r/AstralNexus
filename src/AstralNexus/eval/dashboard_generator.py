"""Dashboard generator: creates HTML visualization for Phase 4 evaluation.

Generates a self-contained HTML dashboard with:
- Semantic task graph visualization
- Dynamic sparse topology
- Ready-wave parallel batches
- Role execution status
- Message compression and fact recall curves
- Fault injection/recovery timeline
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from AstralNexus.core.output_paths import RunOutputLayout


def generate_dashboard(metrics: Dict[str, Any], run_dir: Path) -> Path:
    """Generate HTML dashboard from metrics.

    Args:
        metrics: Aggregated metrics from MetricsAggregator
        run_dir: Directory to save dashboard.html

    Returns:
        Path to generated dashboard.html
    """
    dashboard_path = RunOutputLayout(run_dir).report("dashboard.html")

    # Extract key data for visualization
    task_graph = metrics.get("task_graph_metrics", {})
    router = metrics.get("router_metrics", {})
    wave = metrics.get("ready_wave_metrics", {})
    compression = metrics.get("compression_metrics", {})
    fault = metrics.get("fault_recovery_metrics", {})
    scheduling = metrics.get("scheduling_metrics", {})
    performance = metrics.get("performance_metrics", {})

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AstralNexus Phase 4 Evaluation Dashboard</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: #333;
    padding: 20px;
}}
.container {{
    max-width: 1400px;
    margin: 0 auto;
}}
h1 {{
    text-align: center;
    color: white;
    margin-bottom: 30px;
    font-size: 2.5em;
    text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
}}
.grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
    gap: 20px;
    margin-bottom: 30px;
}}
.card {{
    background: white;
    border-radius: 12px;
    padding: 25px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
}}
.card h2 {{
    color: #667eea;
    margin-bottom: 15px;
    font-size: 1.3em;
    border-bottom: 2px solid #667eea;
    padding-bottom: 8px;
}}
.metric {{
    display: flex;
    justify-content: space-between;
    padding: 8px 0;
    border-bottom: 1px solid #eee;
}}
.metric:last-child {{ border-bottom: none; }}
.metric-label {{ font-weight: 500; color: #666; }}
.metric-value {{ font-weight: 700; color: #333; }}
.highlight {{ color: #667eea; font-size: 1.1em; }}
.success {{ color: #10b981; }}
.warning {{ color: #f59e0b; }}
.danger {{ color: #ef4444; }}
.chart {{
    width: 100%;
    height: 200px;
    margin-top: 15px;
    background: #f9fafb;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #9ca3af;
}}
.timeline {{
    margin-top: 15px;
}}
.timeline-item {{
    display: flex;
    align-items: center;
    padding: 10px;
    margin-bottom: 8px;
    background: #f9fafb;
    border-radius: 6px;
    border-left: 4px solid #667eea;
}}
.timeline-item.success {{ border-left-color: #10b981; }}
.timeline-item.failure {{ border-left-color: #ef4444; }}
.timestamp {{
    font-size: 0.9em;
    color: #6b7280;
    margin-right: 15px;
}}
</style>
</head>
<body>
<div class="container">
<h1>🚀 AstralNexus Phase 4 Evaluation Dashboard</h1>

<div class="grid">
    <!-- Performance Summary -->
    <div class="card">
        <h2>⚡ Performance Summary</h2>
        <div class="metric">
            <span class="metric-label">Total Elapsed</span>
            <span class="metric-value highlight">{performance.get('total_elapsed_seconds', 0):.2f}s</span>
        </div>
        <div class="metric">
            <span class="metric-label">Total Iterations</span>
            <span class="metric-value">{performance.get('total_iterations', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Completed Tasks</span>
            <span class="metric-value success">{performance.get('completed_tasks', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Failed Tasks</span>
            <span class="metric-value {('danger' if performance.get('failed_tasks', 0) > 0 else '')}">{performance.get('failed_tasks', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Recovery Attempts</span>
            <span class="metric-value warning">{performance.get('recovery_attempts', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Throughput</span>
            <span class="metric-value highlight">{performance.get('throughput_tasks_per_second', 0):.2f} tasks/s</span>
        </div>
    </div>

    <!-- Task Graph -->
    <div class="card">
        <h2>📊 Semantic Task Graph</h2>
        <div class="metric">
            <span class="metric-label">Total Nodes</span>
            <span class="metric-value highlight">{task_graph.get('total_nodes', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Total Edges</span>
            <span class="metric-value">{task_graph.get('total_edges', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Dependency Edges</span>
            <span class="metric-value">{task_graph.get('dependency_edges', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Communication Edges</span>
            <span class="metric-value">{task_graph.get('communication_edges', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Avg Dependencies/Node</span>
            <span class="metric-value">{task_graph.get('avg_dependencies_per_node', 0):.2f}</span>
        </div>
    </div>

    <!-- Router Topology -->
    <div class="card">
        <h2>🔀 Router Topology</h2>
        <div class="metric">
            <span class="metric-label">Sparsity Samples</span>
            <span class="metric-value">{router.get('samples', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Avg Sparsity</span>
            <span class="metric-value highlight">{router.get('avg_sparsity', 0):.4f}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Min Sparsity</span>
            <span class="metric-value">{router.get('min_sparsity', 0):.4f}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Max Sparsity</span>
            <span class="metric-value">{router.get('max_sparsity', 0):.4f}</span>
        </div>
        <canvas id="sparsityChart" height="100"></canvas>
    </div>

    <!-- Ready-Wave -->
    <div class="card">
        <h2>🌊 Ready-Wave Execution</h2>
        <div class="metric">
            <span class="metric-label">Total Waves</span>
            <span class="metric-value highlight">{wave.get('total_waves', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Avg Wave Size</span>
            <span class="metric-value">{wave.get('avg_wave_size', 0):.2f}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Max Wave Size</span>
            <span class="metric-value">{wave.get('max_wave_size', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Min Wave Size</span>
            <span class="metric-value">{wave.get('min_wave_size', 0)}</span>
        </div>
        <canvas id="waveChart" height="100"></canvas>
    </div>

    <!-- Compression -->
    <div class="card">
        <h2>🗜️ Context Compression</h2>
        <div class="metric">
            <span class="metric-label">Compression Events</span>
            <span class="metric-value highlight">{compression.get('compression_count', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Avg Ratio</span>
            <span class="metric-value">{compression.get('avg_ratio', 0):.4f}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Natural Triggers</span>
            <span class="metric-value success">{compression.get('trigger_distribution', {}).get('natural', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Forced Triggers</span>
            <span class="metric-value warning">{compression.get('trigger_distribution', {}).get('forced', 0)}</span>
        </div>
        <canvas id="compressionChart" height="100"></canvas>
    </div>

    <!-- Fault Recovery -->
    <div class="card">
        <h2>🛡️ Fault Recovery</h2>
        <div class="metric">
            <span class="metric-label">Total Injections</span>
            <span class="metric-value highlight">{fault.get('total_injections', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Recovery Attempts</span>
            <span class="metric-value">{fault.get('total_recovery_attempts', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Recovery Successes</span>
            <span class="metric-value success">{fault.get('total_recovery_successes', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Success Rate</span>
            <span class="metric-value {('success' if fault.get('recovery_success_rate', 0) >= 0.8 else 'danger')}">{fault.get('recovery_success_rate', 0):.2%}</span>
        </div>
        <div class="timeline">
"""

    # Add fault type distribution
    for ft, count in fault.get("fault_type_distribution", {}).items():
        html += f"""            <div class="timeline-item">
                <span class="timestamp">{ft}</span>
                <span>{count} injection(s)</span>
            </div>
"""

    html += f"""        </div>
    </div>

    <!-- Scheduling -->
    <div class="card">
        <h2>📡 Resource Scheduling</h2>
        <div class="metric">
            <span class="metric-label">Total Decisions</span>
            <span class="metric-value highlight">{scheduling.get('total_scheduling_decisions', 0)}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Local Ratio</span>
            <span class="metric-value">{scheduling.get('local_ratio', 0):.2%}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Edge Ratio</span>
            <span class="metric-value">{scheduling.get('edge_ratio', 0):.2%}</span>
        </div>
        <div class="metric">
            <span class="metric-label">Cloud Ratio</span>
            <span class="metric-value">{scheduling.get('cloud_ratio', 0):.2%}</span>
        </div>
        <canvas id="schedulingChart" height="100"></canvas>
    </div>

    <!-- Role Distribution -->
    <div class="card">
        <h2>👥 Role Distribution</h2>
"""

    for role, count in task_graph.get("role_distribution", {}).items():
        html += f"""        <div class="metric">
            <span class="metric-label">{role}</span>
            <span class="metric-value">{count}</span>
        </div>
"""

    html += """    </div>
</div>

</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
// Sparsity Chart
new Chart(document.getElementById('sparsityChart'), {
    type: 'line',
    data: {
        labels: ['Sample 1', 'Sample 2', 'Sample 3', 'Final'],
        datasets: [{
            label: 'Sparsity Ratio',
            data: [""" + f"{router.get('min_sparsity', 0):.4f}, {router.get('avg_sparsity', 0):.4f}, {router.get('avg_sparsity', 0):.4f}, {router.get('max_sparsity', 0):.4f}" + """],
            borderColor: '#667eea',
            backgroundColor: 'rgba(102, 126, 234, 0.1)',
            fill: true
        }]
    },
    options: { responsive: true, plugins: { legend: { display: false } } }
});

// Wave Chart
new Chart(document.getElementById('waveChart'), {
    type: 'bar',
    data: {
        labels: ['Min', 'Avg', 'Max'],
        datasets: [{
            label: 'Wave Size',
            data: [""" + f"{wave.get('min_wave_size', 0)}, {wave.get('avg_wave_size', 0):.2f}, {wave.get('max_wave_size', 0)}" + """],
            backgroundColor: ['#10b981', '#667eea', '#f59e0b']
        }]
    },
    options: { responsive: true, plugins: { legend: { display: false } } }
});

// Compression Chart
new Chart(document.getElementById('compressionChart'), {
    type: 'doughnut',
    data: {
        labels: ['Natural', 'Forced'],
        datasets: [{
            data: [""" + f"{compression.get('trigger_distribution', {}).get('natural', 0)}, {compression.get('trigger_distribution', {}).get('forced', 0)}" + """],
            backgroundColor: ['#10b981', '#f59e0b']
        }]
    },
    options: { responsive: true }
});

// Scheduling Chart
new Chart(document.getElementById('schedulingChart'), {
    type: 'doughnut',
    data: {
        labels: ['Local', 'Edge', 'Cloud'],
        datasets: [{
            data: [""" + f"{scheduling.get('local_ratio', 0) * 100:.2f}, {scheduling.get('edge_ratio', 0) * 100:.2f}, {scheduling.get('cloud_ratio', 0) * 100:.2f}" + """],
            backgroundColor: ['#667eea', '#10b981', '#f59e0b']
        }]
    },
    options: { responsive: true }
});
</script>
</body>
</html>"""

    dashboard_path.write_text(html, encoding="utf-8")
    return dashboard_path
