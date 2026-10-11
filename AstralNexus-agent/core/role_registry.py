from __future__ import annotations

from AstralNexus.core.agent_registry import AgentRole, AgentRegistry, get_agent_registry


def initialize_role_registry(target: AgentRegistry | None = None) -> None:
    """Initialize the role registry with predefined roles."""
    registry = target or get_agent_registry()
    preserve = target is not None

    def register_factory(name, factory):
        if not preserve or registry.get_role_factory(name) is None:
            registry.register_role_factory(name, factory)

    
    # Define the core roles for the AstralNexus system
    roles = [
        AgentRole(
            name="planner",
            description="规划分析 Worker，向 GlobalGovernor 提供拆解与依赖证据",
            default_tools=["file_read", "search_files", "find_skills", "skills_list", "skill_view"],
            capabilities=["decomposition_analysis", "dependency_analysis", "planning_evidence"],
            input_contract="高层目标、硬约束、验收标准、风险、数据敏感等级、时间/成本预算",
            output_contract="候选拆解、依赖分析、风险与验收建议；不得写任务图"
        ),
        AgentRole(
            name="researcher",
            description="研究员，负责资料检索、文档阅读和证据抽取",
            default_tools=["web_search", "http_get", "file_read", "skills_list", "skill_view"],
            capabilities=["information_retrieval", "document_analysis", "evidence_extraction"],
            input_contract="研究主题、资料来源、关键问题、证据要求",
            output_contract="结构化事实、证据链、冲突点、参考资料列表"
        ),
        AgentRole(
            name="coder",
            description="编码员，负责代码修改、脚本编写和修复实现",
            default_tools=["file_read", "file_write", "terminal_exec", "search_files", "python_exec", "pytest_run", "skills_list", "skill_view", "lsp_query", "runtime_plugins"],
            capabilities=["programming", "code_modification", "scripting", "debugging"],
            input_contract="功能需求、代码规范、测试要求、修改范围",
            output_contract="修改后的代码、新增测试、执行日志、验证结果"
        ),
        AgentRole(
            name="architect",
            description="架构师，负责需求边界、系统结构、接口、数据模型、状态机和权限设计",
            default_tools=["file_read", "file_write", "search_files", "terminal_exec", "python_exec"],
            capabilities=["system_architecture", "requirement_analysis", "api_design", "database_modeling", "state_machine_design", "permission_design"],
            input_contract="目标、需求、约束、既有产物和验收条件",
            output_contract="可供实现节点消费的架构、接口、数据模型、状态机和权限产物",
        ),
        AgentRole(
            name="tester",
            description="测试执行员，负责创建并真实运行单元、集成、端到端和回归测试",
            default_tools=["file_read", "file_write", "search_files", "terminal_exec", "python_exec", "pytest_run", "http_get"],
            capabilities=["test_generation", "test_execution", "unit_testing", "integration_testing", "end_to_end_testing", "api_testing"],
            input_contract="实现产物、测试契约、验收标准和运行环境",
            output_contract="测试代码、机器可读结果、失败证据和运行记录",
        ),
        AgentRole(
            name="synthesizer",
            description="交付整合员，负责汇总已验证产物、生成清单并组装最终交付包",
            default_tools=["file_read", "file_write", "search_files", "terminal_exec", "python_exec", "pytest_run"],
            capabilities=["artifact_integration", "delivery_packaging", "delivery_manifest_generation", "cross_artifact_consistency", "final_synthesis"],
            input_contract="交付契约、Verifier报告、已验证产物和剩余缺口",
            output_contract="交付清单、完整交付包、完成摘要和未解决项",
        ),
        AgentRole(
            name="verifier",
            description="验证证据员，负责执行测试并生成供核心 Verifier 裁决的证据",
            default_tools=["terminal_exec", "file_read", "python_exec", "pytest_run", "skills_list", "skill_view"],
            capabilities=["testing", "verification", "quality_assurance"],
            input_contract="测试计划、验收标准、待验证产物、质量要求",
            output_contract="测试记录、可复现实验、问题证据；不得直接改变任务状态或签发验收结论"
        ),
        AgentRole(
            name="reviewer",
            description="审查员，负责风险审查、接口兼容性和证据一致性检查",
            default_tools=["file_read", "search_files", "skills_list", "skill_view"],
            capabilities=["risk_assessment", "compatibility_checking", "consistency_verification"],
            input_contract="待审查内容、风险关注点、兼容性要求、一致性标准",
            output_contract="风险评估报告、兼容性检查结果、一致性验证结论、改进建议"
        ),
        AgentRole(
            name="memory_curator",
            description="记忆管理员，负责事实沉淀、冲突消解和记忆唤醒",
            default_tools=["file_read", "search_files", "skills_list", "skill_view"],
            capabilities=["knowledge_management", "memory_organization", "conflict_resolution"],
            input_contract="待整理事实、记忆查询请求、冲突检测要求",
            output_contract="结构化事实库、记忆唤醒结果、冲突解决方案"
        ),
        AgentRole(
            name="resource_broker",
            description="资源分析 Worker，提供资源遥测、模型基准和调度选项",
            default_tools=["skills_list", "skill_view", "find_skills"],
            capabilities=["resource_telemetry", "model_benchmarking", "cost_analysis"],
            input_contract="任务属性、资源状态、成本约束、性能要求",
            output_contract="资源遥测、模型基准、候选调度方案；不得签发资源决策"
        ),
        AgentRole(
            name="recovery_agent",
            description="恢复分析 Worker，负责故障诊断、影响证据和恢复选项提案",
            default_tools=["file_read", "file_write", "search_files", "terminal_exec", "python_exec", "pytest_run", "skills_list", "skill_view"],
            capabilities=["fault_diagnosis", "impact_analysis", "recovery_option_analysis"],
            input_contract="故障类型、故障目标、系统状态、恢复约束",
            output_contract="故障分析报告、影响范围、候选恢复选项与证据"
        ),
        AgentRole(
            name="skill_curator",
            description="技能策展者，负责开放 Skill 发现、来源审计、安装和生命周期管理",
            default_tools=["find_skills", "skills_list", "skill_view", "skill_list", "skill_audit", "skill_install", "skill_update", "skill_enable", "skill_disable"],
            capabilities=["skill_discovery", "skill_audit", "skill_lifecycle"],
            input_contract="能力缺口、查询目标、来源约束、安装策略",
            output_contract="候选 Skill、来源、审计结论、安装计划或已验证 Skill",
        ),
    ]
    
    # Register all roles
    for role in roles:
        if not preserve or registry.get_role(role.name) is None:
            registry.register_role(role)

    # Register production role factories so the orchestrator does not depend on
    # class-name fallback imports during heterogeneous role execution.
    from AstralNexus.agents.coder import Coder
    from AstralNexus.agents.architect import Architect
    from AstralNexus.agents.memory_curator import MemoryCurator
    from AstralNexus.agents.planner import PlannerAgent
    from AstralNexus.agents.recovery_agent import RecoveryAgent
    from AstralNexus.agents.researcher import Researcher
    from AstralNexus.agents.resource_broker import ResourceBroker
    from AstralNexus.agents.reviewer import Reviewer
    from AstralNexus.agents.verifier import VerificationWorker
    from AstralNexus.agents.writer import Writer
    from AstralNexus.agents.tester import Tester
    from AstralNexus.agents.synthesizer import Synthesizer
    from AstralNexus.agents.skill_curator import SkillCurator

    factories = {
        "planner": PlannerAgent,
        "researcher": Researcher,
        "coder": Coder,
        "architect": Architect,
        "tester": Tester,
        "synthesizer": Synthesizer,
        "verifier": VerificationWorker,
        "reviewer": Reviewer,
        "writer": Writer,
        "memory_curator": MemoryCurator,
        "resource_broker": ResourceBroker,
        "recovery_agent": RecoveryAgent,
        "skill_curator": SkillCurator,
    }
    for role_name, factory in factories.items():
        register_factory(role_name, factory)

    # Capability ontology aliases map Governor profile names to executable
    # factories. They do not create an alternate execution path.
    aliases = {
        "recovery": "recovery_agent",
        "recovery agent": "recovery_agent",
        "integrator": "reviewer",
        "documenter": "writer",
        "testing": "tester",
        "architect": "architect",
        "synthesizer": "synthesizer",
        # Governor worker-profile names are capability-level identities.  They
        # must resolve to a concrete production role factory at instantiation
        # time while the selected worker_type remains in metadata/audit logs.
        # This is deliberately explicit: it prevents a profile name from
        # being mistaken for an executable role and prevents generic fallback.
        "retrieval": "researcher",
        "research": "researcher",
        "reasoning": "reviewer",
        "coding": "coder",
        "architecture": "architect",
        "tool": "resource_broker",
        "vision": "researcher",
        "device": "resource_broker",
        "critic": "reviewer",
        "verifier": "verifier",
        "testing_worker": "tester",
        "synthesis": "synthesizer",
        "recovery_worker": "recovery_agent",
        # Heterogeneous capability labels (Retrieval/Reasoning/Vision/Coding/Tool/Device/Critic)
        # capability labels are resolved through these aliases before a
        # concrete role factory is instantiated.
    }
    for alias, canonical in aliases.items():
        registry.register_role_alias(alias, canonical)


def get_role_capabilities(role_name: str) -> list:
    """Get the capabilities of a role.
    
    Args:
        role_name: The name of the role
        
    Returns:
        List of capabilities for the role, or empty list if not found
    """
    registry = get_agent_registry()
    role = registry.get_role(role_name)
    if role:
        return role.capabilities
    return []
