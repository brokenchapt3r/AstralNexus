"""
Performance benchmark script for AstralNexus.
Tests scalability, throughput, and latency characteristics.
"""
import sys
import time
import random
import json
from pathlib import Path
from typing import List, Dict, Any

# Add both project root and AstralNexus package root to path
_project_root = str(Path(__file__).resolve().parents[1])
_package_root = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, _project_root)
sys.path.insert(0, _package_root)

from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.value_router import RoutedMessageEnvelope, ValueRouter
from AstralNexus.runtime.context_compressor import ContextCompressor


class PerformanceBenchmark:
    def __init__(self):
        self.results: List[Dict[str, Any]] = []
    
    def record(self, name: str, value: float, unit: str = "ms"):
        self.results.append({"name": name, "value": value, "unit": unit})
        print(f"  {name}: {value:.2f} {unit}")
    
    def test_task_graph_construction(self, node_counts=[10, 50, 100, 200]):
        print("\n=== Task Graph Construction ===")
        for n in node_counts:
            graph = TaskGraph()
            start = time.perf_counter()
            for i in range(n):
                deps = [f"node_{d}" for d in random.sample(range(max(0, i-5), i), min(2, i))]
                node = TaskNode(
                    node_id=f"node_{i}",
                    goal=f"Task {i}",
                    role="worker",
                    dependencies=deps,
                    constraints=["constraint_1"]
                )
                graph.add_node(node)
            elapsed = (time.perf_counter() - start) * 1000
            self.record(f"construct_{n}_nodes", elapsed, "ms")
            assert len(graph.nodes) == n
    
    def test_value_router(self, message_counts=[10, 50, 100, 500]):
        print("\n=== Value Router (Message Filtering) ===")
        for m in message_counts:
            graph = TaskGraph()
            # Add nodes first
            for i in range(10):
                graph.add_node(TaskNode(node_id=f"node_{i}", goal=f"Task {i}", role="worker"))
            
            router = ValueRouter(graph, value_threshold=0.3)
            transmitted = 0
            dropped = 0
            
            start = time.perf_counter()
            for i in range(m):
                msg = RoutedMessageEnvelope(
                    message_id=f"msg_{i}",
                    sender=f"node_{random.randint(0, 9)}",
                    receiver=f"node_{random.randint(0, 9)}",
                    msg_type="evidence_update",
                    task_id="task_1",
                    claims=[{"claim": f"Result {i}", "confidence": random.random()}],
                    evidence_refs=[f"evidence_{i}"],
                    information_gain=random.random(),
                    relevance=random.random(),
                    urgency=random.random(),
                    transmission_cost=0.1
                )
                if router.route_message(msg):
                    transmitted += 1
                else:
                    dropped += 1
            
            elapsed = (time.perf_counter() - start) * 1000
            self.record(f"route_{m}_messages", elapsed, "ms")
            self.record(f"transmitted_{m}", transmitted, "count")
            self.record(f"dropped_{m}", dropped, "count")
    
    def test_context_compression(self, message_counts=[20, 50, 100]):
        print("\n=== Context Compression ===")
        for m in message_counts:
            compressor = ContextCompressor(threshold=0.5, target_ratio=0.6)
            messages = [
                {"role": "user", "content": f"User message {i} " + "word " * random.randint(20, 100)}
                for i in range(m)
            ]
            
            context_length = 8000
            should_compress = compressor.should_compress(messages, context_length)
            
            start = time.perf_counter()
            if should_compress:
                compressed_messages, stats = compressor.compress(messages, context_length)
                elapsed = (time.perf_counter() - start) * 1000
                self.record(f"compress_{m}_messages", elapsed, "ms")
                if stats.get("compressed"):
                    before = stats.get("before_tokens_estimate", 0)
                    after = stats.get("after_tokens_estimate", 0)
                    ratio = after / before if before > 0 else 1.0
                    self.record(f"compression_ratio_{m}", ratio * 100, "%")
                else:
                    self.record(f"compression_ratio_{m}", 100.0, "% (no compression)")
            else:
                elapsed = (time.perf_counter() - start) * 1000
                self.record(f"compress_{m}_messages", elapsed, "ms (below threshold)")
                self.record(f"compression_ratio_{m}", 100.0, "% (no compression)")
    
    def run_all(self):
        print("=" * 60)
        print("AstralNexus Performance Benchmark")
        print("=" * 60)
        
        self.test_task_graph_construction()
        self.test_value_router()
        self.test_context_compression()
        
        # Summary
        print("\n" + "=" * 60)
        print("Summary")
        print("=" * 60)
        for result in self.results:
            print(f"{result['name']}: {result['value']:.2f} {result['unit']}")
        
        # Save results
        output_dir = Path(__file__).resolve().parents[1] / "runs" / "benchmarks"
        output_dir.mkdir(exist_ok=True)
        output_file = output_dir / f"benchmark_{int(time.time())}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)
        print(f"\nResults saved to: {output_file}")
        
        return self.results


if __name__ == "__main__":
    benchmark = PerformanceBenchmark()
    benchmark.run_all()
