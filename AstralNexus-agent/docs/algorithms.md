# 天枢核心算法文档

## 概述

本文档详细描述了天枢系统中使用的核心算法，包括动态稀疏拓扑路由、最小上下文检索和受影响子图重算等。

## 1. 动态稀疏拓扑路由算法

### 1.1 算法描述

动态稀疏拓扑路由算法是天枢系统的核心，它决定了在任何给定时刻哪些任务节点应该被执行，由哪个角色执行，使用什么资源，以及如何与其他节点通信。

### 1.2 输入输出

**输入**：
- 全局目标 G
- 任务图 DAG
- 角色池 R
- 资源状态 C
- 任务记忆 M
- 故障状态 F

**输出**：
- 下一轮执行节点集合
- 角色分配
- 通信边
- 资源选择

### 1.3 算法伪代码

```
while not done(DAG):
    frontier = select_ready_nodes(DAG)

    for node in frontier:
        features = encode(
            node.goal,
            node.dependencies,
            node.risk_level,
            node.sensitivity,
            recent_faults(F),
            upstream_artifacts(DAG, node)
        )

        role = choose_role(features, R)
        resource = choose_resource(node, role, C)
        context = retrieve_minimal_context(node, M, DAG)
        contract = build_task_contract(node, role, context, resource)

        dispatch(node, role, resource, contract)

    results = collect_results()

    for result in results:
        if not validate_schema(result):
            create_recovery_node(result, reason="schema_error")
            continue

        update_facts(M, result.claims)
        update_artifacts(DAG, result.artifacts)
        update_metrics(result.budget_used)

        if result.status in ["fail", "blocked"]:
            create_recovery_node(result)
        else:
            expand_or_close_node(result.next_actions)

    prune_low_value_edges(DAG)
    refresh_sparse_topology(DAG)
```

### 1.4 复杂度分析

令：
- 任务节点数为 N
- 候选角色数为 R
- 候选资源数为 C
- 候选边数为 E
- 每轮可执行节点数为 K

复杂度分析：
1. frontier 选择复杂度为 O(N)
2. 角色匹配复杂度为 O(KR)
3. 资源选择复杂度为 O(KC)
4. 记忆检索若采用 SQLite FTS 或向量索引，约为 O(K(log M + top_k))
5. 稀疏拓扑维护只在依赖邻域与候选通信边中进行，避免全连接 O(N^2)，实际为 O(E_sparse)

因此，通信复杂度从传统全连接多智能体的 O(N^2 * L_free_text) 降为 O(E_sparse * L_structured)，其中 E_sparse << N^2 且 L_structured << L_free_text。

### 1.5 算法优势

1. **降低通信复杂度**：通过稀疏连接大大减少了节点间的通信量
2. **提高执行效率**：只在必要的时候建立连接，避免了不必要的计算
3. **增强可扩展性**：算法复杂度与节点数的关系更加友好
4. **改善资源利用**：根据任务特征动态分配资源

## 2. 最小上下文检索算法

### 2.1 算法描述

最小上下文检索算法的目标是只给子任务注入必要的上下文，避免长程任务中的 token 冗余。

### 2.2 算法伪代码

```
def retrieve_minimal_context(node, memory, graph):
    required_constraints = active_constraints(graph)
    upstream_decisions = decisions_from_dependencies(node)
    relevant_facts = search_facts(node.goal, memory, top_k=8)
    relevant_artifacts = search_artifacts(node.goal, graph, top_k=5)
    recent_failures = failures_affecting(node)

    context = merge(
        required_constraints,
        upstream_decisions,
        relevant_facts,
        relevant_artifacts,
        recent_failures
    )

    return compress_to_budget(context, node.context_budget)
```

### 2.3 关键指标

1. **上下文压缩率**：衡量上下文被压缩的程度
2. **关键约束召回率**：衡量关键约束是否被正确检索
3. **事实引用准确率**：衡量检索到的事实的相关性
4. **目标漂移率**：衡量任务目标在压缩过程中是否发生变化

### 2.4 算法优势

1. **节省Token**：通过压缩上下文减少Token消耗
2. **保持相关性**：确保检索到的信息与任务相关
3. **防止信息丢失**：通过多种检索策略确保关键信息不丢失
4. **动态适应**：根据任务需求动态调整上下文内容

## 3. 受影响子图重算算法

### 3.1 算法描述

当发生需求变更或节点失败时，受影响子图重算算法能够定位受影响的部分并只重新计算这些部分，而不是重新计算整个任务图。

### 3.2 算法伪代码

```
def recover_from_fault(fault, graph):
    impacted = mark_impacted_nodes(fault.target, graph)
    frozen = graph.nodes - impacted

    for node in impacted:
        node.status = "pending_recovery"

    recovery_plan = plan_recovery(fault, impacted, frozen)
    add_recovery_nodes(graph, recovery_plan)
    reroute(impacted)
```

### 3.3 复杂度分析

令：
- 受影响节点数为 S
- 依赖边数为 E_s

恢复复杂度约为 O(S + E_s)，优于全图重算 O(N + E)。

### 3.4 算法优势

1. **提高效率**：只重新计算受影响的部分，大大提高恢复速度
2. **节约资源**：避免不必要的计算，节约计算资源
3. **保持一致性**：未受影响的部分保持不变，确保任务的一致性
4. **快速响应**：能够快速响应故障并进行恢复

## 4. 角色选择算法

### 4.1 算法描述

角色选择算法根据任务特征和角色能力为任务节点选择最合适的角色。

### 4.2 算法实现

```
def choose_role(features, role_pool):
    scores = {}
    for role in role_pool:
        # 计算角色与任务特征的匹配度
        capability_score = calculate_capability_match(features, role.capabilities)
        experience_score = calculate_experience_bonus(role, features)
        availability_score = calculate_availability(role)
        
        # 综合得分
        total_score = (
            0.5 * capability_score +
            0.3 * experience_score +
            0.2 * availability_score
        )
        scores[role.name] = total_score
    
    # 选择得分最高的角色
    best_role = max(scores, key=scores.get)
    return best_role
```

### 4.3 评分因素

1. **能力匹配度**（权重0.5）：角色能力与任务需求的匹配程度
2. **经验加成**（权重0.3）：角色在类似任务上的历史表现
3. **可用性**（权重0.2）：角色当前是否可用

## 5. 资源调度算法

### 5.1 算法描述

资源调度算法根据任务属性、资源状态和调度策略为任务节点选择最合适的计算资源。

### 5.2 算法实现

```
def choose_resource(node, role, resource_pool):
    scores = {}
    for resource in resource_pool:
        # 计算资源得分
        capability_match = calculate_capability_match(node, role, resource)
        context_fit = calculate_context_fit(node, resource)
        reliability = calculate_reliability(resource)
        privacy_penalty = calculate_privacy_penalty(node, resource)
        latency_penalty = calculate_latency_penalty(node, resource)
        cost_penalty = calculate_cost_penalty(node, resource)
        
        # 综合得分
        total_score = (
            capability_match +
            context_fit +
            reliability -
            privacy_penalty -
            latency_penalty -
            cost_penalty
        )
        scores[resource.id] = total_score
    
    # 选择得分最高的资源
    best_resource = max(scores, key=scores.get)
    return best_resource
```

### 5.3 评分因素

1. **能力匹配**：资源能力与任务需求的匹配程度
2. **上下文适配**：资源是否适合处理当前任务的上下文大小
3. **可靠性**：资源的历史可靠性和稳定性
4. **隐私惩罚**：处理敏感数据时的隐私保护程度
5. **延迟惩罚**：资源的响应延迟
6. **成本惩罚**：使用资源的成本

## 6. 事实检索算法

### 6.1 算法描述

事实检索算法用于从记忆系统中检索与当前任务相关的事实。

### 6.2 算法实现

```
def search_facts(query, memory, top_k=8):
    # 在不同记忆层中检索
    short_term_results = search_memory_layer(memory.short_term, query)
    medium_term_results = search_memory_layer(memory.medium_term, query)
    long_term_results = search_memory_layer(memory.long_term, query)
    
    # 合并结果并排序
    all_results = short_term_results + medium_term_results + long_term_results
    ranked_results = rank_results(all_results, query)
    
    # 返回前top_k个结果
    return ranked_results[:top_k]

def rank_results(results, query):
    # 基于相关性、置信度和新鲜度进行排序
    for result in results:
        relevance = calculate_relevance(result.content, query)
        freshness = calculate_freshness(result.created_at)
        result.score = (
            0.6 * relevance +
            0.3 * result.confidence +
            0.1 * freshness
        )
    
    return sorted(results, key=lambda x: x.score, reverse=True)
```

### 6.3 排序因素

1. **相关性**（权重0.6）：事实内容与查询的相关程度
2. **置信度**（权重0.3）：事实的可信度
3. **新鲜度**（权重0.1）：事实的新近程度

## 7. 故障检测与分析算法

### 7.1 算法描述

故障检测与分析算法用于检测系统中的各种故障并分析其影响范围。

### 7.2 算法实现

```
def detect_and_analyze_faults(task_graph, trace_logs):
    faults = []
    
    # 检测各种类型的故障
    requirement_changes = detect_requirement_changes(trace_logs)
    node_failures = detect_node_failures(trace_logs)
    tool_failures = detect_tool_failures(trace_logs)
    memory_losses = detect_memory_losses(trace_logs)
    resource_degradations = detect_resource_degradations(trace_logs)
    
    # 合并所有故障
    all_faults = (
        requirement_changes +
        node_failures +
        tool_failures +
        memory_losses +
        resource_degradations
    )
    
    # 分析每个故障的影响
    for fault in all_faults:
        impact_analysis = analyze_impact(fault, task_graph)
        fault.impact_analysis = impact_analysis
        faults.append(fault)
    
    return faults

def analyze_impact(fault, task_graph):
    # 根据故障类型分析影响
    if fault.type == "requirement_change":
        return analyze_requirement_change_impact(fault, task_graph)
    elif fault.type == "node_failure":
        return analyze_node_failure_impact(fault, task_graph)
    elif fault.type == "tool_failure":
        return analyze_tool_failure_impact(fault, task_graph)
    # ... 其他故障类型的分析
```

## 8. 恢复计划生成算法

### 8.1 算法描述

恢复计划生成算法根据故障分析结果生成具体的恢复计划。

### 8.2 算法实现

```
def plan_recovery(fault, impact_analysis):
    recovery_actions = []
    
    # 根据故障类型和影响分析生成恢复动作
    if fault.type == "requirement_change":
        recovery_actions = plan_requirement_change_recovery(impact_analysis)
    elif fault.type == "node_failure":
        recovery_actions = plan_node_failure_recovery(impact_analysis)
    elif fault.type == "tool_failure":
        recovery_actions = plan_tool_failure_recovery(impact_analysis)
    # ... 其他故障类型的恢复计划
    
    # 优化恢复动作顺序
    optimized_actions = optimize_recovery_sequence(recovery_actions)
    
    return optimized_actions

def optimize_recovery_sequence(actions):
    # 根据依赖关系和资源可用性优化执行顺序
    # 使用拓扑排序或其他调度算法
    return topological_sort(actions)
```

## 总结

天枢系统通过这些核心算法实现了高效的动态异构群体智能协同推理。这些算法相互配合，共同构成了系统的智能决策和执行能力，使得系统能够在复杂的任务环境中自主地进行规划、执行、监控和恢复。