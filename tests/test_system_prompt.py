"""Contract tests for AstralNexus.runtime.system_prompt (dsh-system-prompt port).

Covers: tool order validation/application, strict {{variable}} interpolation
(malformed/unknown/no-value diagnostics, literal lone-{{ prose, no rescanning
of substituted values), context snapshot rendering with the standing
supersession prefix, zod-equivalent config coercion, the default constructor
registrations (identity + persona slots + suppressor), the aggregate
PromptLayer registry over scoped layers (shadowing, reclamation, change
emission), and assemble (ordering, deep-copied parameters, deferLoading
retention, knownNames fallback, suppression from any layer, complete-section
restoration after the interceptor waterfall).
"""
from __future__ import annotations

import pytest

from AstralNexus.runtime.scope import ScopedContext, create_scope, new_scope_key
from AstralNexus.runtime.system_prompt import (
    CONTEXT_ORDERS, PERSONA_PREFIX_SECTION, PERSONA_SUFFIX_SECTION, SECTION_ORDERS,
    TOOL_ORDER_REST, SystemPrompt, join_context_sections, order_tools, render_context_sections,
    render_context_snapshot, render_prompt, validate_config, validate_tool_order)


class EmittingCtx:
    """A fake root context with the emit seam the registry forwards into."""

    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event: str) -> None:
        self.events.append(event)


def tool(name: str, description: str = "d", parameters: dict | None = None,
         **extra) -> dict:
    schema = {"name": name, "description": description,
              "parameters": parameters if parameters is not None else {"type": "object"}}
    schema.update(extra)
    return schema


def provider(*tools, known_names=None, defer_key=None):
    def supply(_context):
        result = {"schemas": list(tools)}
        if known_names is not None:
            result["knownNames"] = known_names
        return result
    return supply


class TestValidateToolOrder:
    def test_none_passes_through(self) -> None:
        assert validate_tool_order(None) is None

    def test_duplicates_refused(self) -> None:
        with pytest.raises(ValueError, match='toolOrder lists "read" more than once'):
            validate_tool_order(["read", TOOL_ORDER_REST, "read"])

    def test_rest_marker_required(self) -> None:
        with pytest.raises(ValueError, match="rest entry"):
            validate_tool_order(["read", "write"])

    def test_valid_order_returned(self) -> None:
        order = ["read", TOOL_ORDER_REST, "write"]
        assert validate_tool_order(order) is order


class TestOrderTools:
    def test_no_configured_order_is_codeunit_lexicographic(self) -> None:
        tools = [tool("Zebra"), tool("alpha"), tool("Beta")]
        assert [t["name"] for t in order_tools(tools, None, set())] == \
            ["Beta", "Zebra", "alpha"]  # code-unit: uppercase before lowercase

    def test_reserved_name_from_provider_fails_assembly(self) -> None:
        with pytest.raises(ValueError, match="reserved tool name"):
            order_tools([tool(TOOL_ORDER_REST)], None, set())

    def test_unlisted_insert_lexicographically_at_rest_slot(self) -> None:
        tools = [tool("m"), tool("a"), tool("z"), tool("listed")]
        ordered = order_tools(tools, ["listed", TOOL_ORDER_REST], {"listed"})
        assert [t["name"] for t in ordered] == ["listed", "a", "m", "z"]

    def test_unknown_configured_names_fail_with_known_diagnostic(self) -> None:
        with pytest.raises(ValueError, match='unregistered tools "ghost1", "ghost2"') as exc:
            order_tools([tool("real")], ["ghost1", "ghost2", TOOL_ORDER_REST], {"real"})
        assert "known tools: real" in str(exc.value)

    def test_unknown_single_and_empty_known_list(self) -> None:
        with pytest.raises(ValueError, match="unregistered tool \"ghost\""):
            order_tools([], ["ghost", TOOL_ORDER_REST], set())
        with pytest.raises(ValueError, match=r"known tools: \(none\)"):
            order_tools([], ["ghost", TOOL_ORDER_REST], set())

    def test_listed_but_absent_tools_are_simply_skipped(self) -> None:
        # "known but restricted names may be absent"
        ordered = order_tools([tool("b")], ["a", "b", TOOL_ORDER_REST], {"a", "b"})
        assert [t["name"] for t in ordered] == ["b"]


class TestInterpolation:
    def assembly(self, sections, variables=None, contexts=None):
        return {"sections": sections, "variables": variables or {},
                "contexts": contexts or []}

    def test_plain_interpolation_and_empty_drop_and_join(self) -> None:
        rendered = render_prompt(self.assembly(
            [{"name": "a", "text": "hello {{who}}"},
             {"name": "b", "text": ""},
             {"name": "c", "text": "bye"}],
            {"who": "world"}))
        assert rendered == "hello world\n\nbye"

    def test_interpolate_false_keeps_literal_text(self) -> None:
        rendered = render_prompt(self.assembly(
            [{"name": "a", "text": "raw {{not_a_var}}", "interpolate": False}], {}))
        assert rendered == "raw {{not_a_var}}"

    def test_lone_open_without_any_later_close_is_prose(self) -> None:
        rendered = render_prompt(self.assembly(
            [{"name": "a", "text": "a {{ b and more prose"}], {}))
        assert rendered == "a {{ b and more prose"

    def test_malformed_group_with_later_close_throws_attributed(self) -> None:
        # "{{ y} " fails GROUP_AT (the inner text hits a lone "}"), and a "}}"
        # exists later in the text → the positional malformed arm fires.
        with pytest.raises(ValueError, match="malformed prompt variable reference at") as exc:
            render_prompt(self.assembly([{"name": "identity", "text": "x {{ y} }} z"}], {}))
        assert 'in section "identity"' in str(exc.value)

    def test_malformed_inner_name_throws_with_grammar_hint(self) -> None:
        with pytest.raises(ValueError, match=r'"\{\{ y \}\}" in section "identity"'):
            render_prompt(self.assembly([{"name": "identity", "text": "x {{ y }} z"}], {}))

    def test_invalid_name_grammar_throws(self) -> None:
        with pytest.raises(ValueError, match="variable names match"):
            render_prompt(self.assembly([{"name": "s", "text": "{{BadName}}"}], {}))

    def test_unknown_variable_lists_registered(self) -> None:
        with pytest.raises(ValueError, match='unknown prompt variable "{{missing}}"') as exc:
            render_prompt(self.assembly([{"name": "s", "text": "{{missing}}"}],
                                        {"known_one": "v"}))
        assert "registered variables: known_one" in str(exc.value)
        with pytest.raises(ValueError, match=r"registered variables: \(none\)"):
            render_prompt(self.assembly([{"name": "s", "text": "{{missing}}"}], {}))

    def test_none_value_has_no_value_error(self) -> None:
        with pytest.raises(ValueError, match="has no value for this assembly"):
            render_prompt(self.assembly([{"name": "s", "text": "{{v}}"}], {"v": None}))

    def test_substituted_values_are_not_rescanned(self) -> None:
        rendered = render_prompt(self.assembly(
            [{"name": "s", "text": "{{payload}}"}],
            {"payload": "{{unknown}} stays literal"}))
        assert rendered == "{{unknown}} stays literal"

    def test_adjacent_groups_and_tail(self) -> None:
        rendered = render_prompt(self.assembly(
            [{"name": "s", "text": "{{a}}{{b}}-tail"}], {"a": "1", "b": "2"}))
        assert rendered == "12-tail"


class TestContextRendering:
    def assembly(self, contexts, variables=None):
        return {"sections": [], "variables": variables or {}, "contexts": contexts}

    def test_sections_drop_empty_and_interpolate(self) -> None:
        sections = render_context_sections(self.assembly(
            [{"name": "sandbox", "text": "policy {{mode}}"},
             {"name": "empty", "text": ""}],
            {"mode": "strict"}))
        assert sections == [{"name": "sandbox", "text": "policy strict"}]

    def test_join_prefixes_the_supersession_sentence(self) -> None:
        joined = join_context_sections([{"name": "a", "text": "one"},
                                        {"name": "b", "text": "two"}])
        assert joined == ("Current runtime context. This snapshot supersedes earlier "
                          "runtime-context snapshots.\n\none\n\ntwo")

    def test_join_empty_is_empty_string(self) -> None:
        assert join_context_sections([]) == ""
        assert render_context_snapshot(self.assembly([])) == ""

    def test_snapshot_composition(self) -> None:
        snapshot = render_context_snapshot(self.assembly(
            [{"name": "approval", "text": "ask mode"}]))
        assert snapshot.endswith("ask mode")
        assert snapshot.startswith("Current runtime context.")

    def test_context_interpolation_errors_attribute_to_context(self) -> None:
        with pytest.raises(ValueError, match='in context "sandbox"'):
            render_context_sections(self.assembly([{"name": "sandbox", "text": "{{nope}}"}]))


class TestConfig:
    def test_defaults(self) -> None:
        resolved = validate_config(None)
        assert resolved == {"include_harness_identity": True, "include_runtime_context": True,
                            "persona_prefix": "", "persona_suffix": "", "tool_order": None}

    def test_unknown_keys_stripped_like_zod(self) -> None:
        resolved = validate_config({"persona_prefix": "P", "bogus": 1})
        assert "bogus" not in resolved
        assert resolved["persona_prefix"] == "P"

    def test_type_checks(self) -> None:
        with pytest.raises(ValueError, match="include_harness_identity must be a boolean"):
            validate_config({"include_harness_identity": "yes"})
        with pytest.raises(ValueError, match="persona_prefix must be a string"):
            validate_config({"persona_prefix": 3})
        with pytest.raises(ValueError, match="tool_order must be a list of strings"):
            validate_config({"tool_order": ["ok", 7]})

    def test_tool_order_validated_on_the_way_in(self) -> None:
        with pytest.raises(ValueError, match="rest entry"):
            validate_config({"tool_order": ["read"]})
        resolved = validate_config({"tool_order": ["read", TOOL_ORDER_REST]})
        assert resolved["tool_order"] == ["read", TOOL_ORDER_REST]


class TestConstructorDefaults:
    def section_names(self, prompt: SystemPrompt) -> list:
        return [entry["name"] for entry in
                prompt.layers.merge(None, lambda layer: layer.sections).values()]

    def test_identity_and_persona_slots_registered_in_canonical_order(self) -> None:
        prompt = SystemPrompt()
        assembly = prompt.assemble()
        names = [section["name"] for section in assembly["sections"]]
        assert names == ["harness:identity", PERSONA_PREFIX_SECTION, PERSONA_SUFFIX_SECTION]
        identity = assembly["sections"][0]
        assert identity["text"] == "You are an AI agent powered by DeepSeek Harness."
        assert set(identity.keys()) == {"name", "text"}  # the projection carries no order

    def test_identity_can_be_excluded(self) -> None:
        prompt = SystemPrompt(config={"include_harness_identity": False})
        assert "harness:identity" not in self.section_names(prompt)

    def test_persona_texts_fill_the_slots(self) -> None:
        prompt = SystemPrompt(config={"persona_prefix": "I am P.", "persona_suffix": "S."})
        assembly = prompt.assemble()
        by_name = {section["name"]: section["text"] for section in assembly["sections"]}
        assert by_name[PERSONA_PREFIX_SECTION] == "I am P."
        assert by_name[PERSONA_SUFFIX_SECTION] == "S."

    def test_runtime_context_suppressed_from_config(self) -> None:
        prompt = SystemPrompt(config={"include_runtime_context": False})
        prompt.context({"name": "c", "order": 1, "text": "never shown"})
        assembly = prompt.assemble()
        assert assembly["contexts"] == []

    def test_change_events_forwarded_to_emit_surface(self) -> None:
        ctx = EmittingCtx()
        prompt = SystemPrompt(ctx)
        constructor_events = ctx.events.count("system-prompt/change")
        assert constructor_events >= 2  # identity + two persona sections
        dispose = prompt.section({"name": "x", "order": 1, "text": "t"})
        assert ctx.events.count("system-prompt/change") == constructor_events + 1
        dispose()
        assert ctx.events.count("system-prompt/change") == constructor_events + 2


class TestRegistration:
    def test_section_requires_finite_order(self) -> None:
        prompt = SystemPrompt()
        with pytest.raises(TypeError, match='prompt section "x" order must be a finite number'):
            prompt.section({"name": "x", "text": "t"})
        with pytest.raises(TypeError, match="finite number"):
            prompt.section({"name": "x", "order": True, "text": "t"})  # bool is not finite number

    def test_context_requires_finite_order(self) -> None:
        prompt = SystemPrompt()
        with pytest.raises(TypeError, match='prompt context "c" order must be a finite number'):
            prompt.context({"name": "c", "text": "t"})

    def test_duplicate_section_diagnostic_points_to_agent_ctx(self) -> None:
        prompt = SystemPrompt()
        prompt.section({"name": "dup", "order": 1, "text": "a"})
        with pytest.raises(ValueError, match="already registered .*agent.ctx") as exc:
            prompt.section({"name": "dup", "order": 2, "text": "b"})
        assert "per-agent override" in str(exc.value)

    def test_scoped_duplicate_diagnostic_names_the_scope(self) -> None:
        key = new_scope_key("agent")
        scope = create_scope(object(), key)
        prompt = SystemPrompt(scope.ctx)
        prompt.section({"name": "dup", "order": 1, "text": "a"})
        with pytest.raises(ValueError, match="already registered in this scope"):
            prompt.section({"name": "dup", "order": 2, "text": "b"})

    def test_variable_name_grammar(self) -> None:
        prompt = SystemPrompt()
        with pytest.raises(ValueError, match="invalid prompt variable name"):
            prompt.variable("Bad", lambda ctx: "x")
        with pytest.raises(ValueError, match="invalid prompt variable name"):
            prompt.variable("9lives", lambda ctx: "x")
        dispose = prompt.variable("ok_1", lambda ctx: "x")
        assert callable(dispose)

    def test_variable_provider_must_be_callable(self) -> None:
        prompt = SystemPrompt()
        with pytest.raises(TypeError, match="variable provider must be callable"):
            prompt.variable("v", "static")  # type: ignore[arg-type]

    def test_tool_provider_must_be_callable(self) -> None:
        prompt = SystemPrompt()
        with pytest.raises(TypeError, match="tool provider must be callable"):
            prompt.tools([])  # type: ignore[arg-type]

    def test_disposer_removes_registration_and_reclaims_scoped_overlay(self) -> None:
        key = new_scope_key("agent")
        scope = create_scope(object(), key)
        prompt = SystemPrompt()  # registry lives at the root
        dispose = prompt.section({"name": "scoped", "order": 1, "text": "t"})
        # registered through the root ctx → global layer
        assert prompt.layers.peek(key) is None
        dispose()
        names = [entry["name"] for entry in
                 prompt.layers.merge(None, lambda layer: layer.sections).values()]
        assert "scoped" not in names
        # now through the scoped ctx: the overlay holds the constructor's
        # default sections too, so assert the entry itself is reclaimed
        scoped_prompt = SystemPrompt(scope.ctx)
        assert scoped_prompt.layers.peek(key) is not None
        dispose_scoped = scoped_prompt.section({"name": "s", "order": 1, "text": "t"})
        overlay = scoped_prompt.layers.peek(key)
        assert overlay is not None
        assert overlay.sections.has("s")
        dispose_scoped()
        assert not overlay.sections.has("s")  # only this registration is removed
        assert overlay is scoped_prompt.layers.peek(key)  # non-empty overlay kept


class TestAssemble:
    def test_sections_sorted_by_order_then_name(self) -> None:
        prompt = SystemPrompt()  # identity(-1000), prefix(0), suffix(10200) are standing
        prompt.section({"name": "b", "order": 10, "text": "B"})
        prompt.section({"name": "a", "order": 10, "text": "A"})
        prompt.section({"name": "early", "order": -5, "text": "E"})
        names = [section["name"] for section in prompt.assemble()["sections"]]
        assert names == ["harness:identity", "early", PERSONA_PREFIX_SECTION,
                         "a", "b", PERSONA_SUFFIX_SECTION]

    def test_callable_text_receives_the_context(self) -> None:
        prompt = SystemPrompt(config={"include_harness_identity": False})
        prompt.section({"name": "dyn", "order": 1,
                        "text": lambda ctx: f'mode={ctx["mode"]}'})
        prompt.context({"name": "c", "order": 1, "text": lambda ctx: ctx["mode"].upper()})
        prompt.variable("v", lambda ctx: ctx["mode"])
        assembly = prompt.assemble({"mode": "strict"})
        texts = {section["name"]: section["text"] for section in assembly["sections"]}
        assert texts["dyn"] == "mode=strict"
        assert assembly["contexts"] == [{"name": "c", "text": "STRICT"}]
        assert assembly["variables"] == {"v": "strict"}

    def test_contexts_sorted_by_order_stable(self) -> None:
        prompt = SystemPrompt()
        prompt.context({"name": "late", "order": 200, "text": "L"})
        prompt.context({"name": "early", "order": 100, "text": "E"})
        contexts = prompt.assemble()["contexts"]
        assert [entry["name"] for entry in contexts] == ["early", "late"]

    def test_tool_parameters_deep_copied_into_the_assembly(self) -> None:
        parameters: dict = {"type": "object", "properties": {"a": {"type": "string"}}}
        prompt = SystemPrompt()
        prompt.tools(provider(tool("t", parameters=parameters)))
        assembly = prompt.assemble()
        assert assembly["tools"][0]["parameters"] == parameters
        assembly["tools"][0]["parameters"]["properties"]["a"]["type"] = "number"
        assert parameters["properties"]["a"]["type"] == "string"  # source untouched

    def test_defer_loading_kept_only_when_true(self) -> None:
        prompt = SystemPrompt()
        prompt.tools(provider(tool("kept", deferLoading=True), tool("dropped", deferLoading=False)))
        tools = {t["name"]: t for t in prompt.assemble()["tools"]}
        assert tools["kept"].get("deferLoading") is True
        assert "deferLoading" not in tools["dropped"]

    def test_known_names_fallback_and_explicit(self) -> None:
        prompt = SystemPrompt(config={"tool_order": ["restricted", "t", TOOL_ORDER_REST]})
        prompt.tools(provider(tool("t"), known_names=["restricted", "t"]))
        names = [t["name"] for t in prompt.assemble()["tools"]]
        assert names == ["t"]  # "restricted" known but absent is fine
        # without the explicit knownNames the restricted name would be unknown:
        strict = SystemPrompt(config={"tool_order": ["restricted", TOOL_ORDER_REST]})
        strict.tools(provider(tool("t")))
        with pytest.raises(ValueError, match='unregistered tool "restricted"'):
            strict.assemble()

    def test_global_and_scoped_providers_all_contribute(self) -> None:
        key = new_scope_key("agent")
        scope = create_scope(object(), key)
        prompt = SystemPrompt()
        prompt.tools(provider(tool("global-tool")))
        # same registry, registered through the scoped context
        prompt.ctx = scope.ctx
        prompt.tools(provider(tool("scoped-tool")))
        prompt.ctx = None
        assembly = prompt.assemble({"scope": key})
        names = {t["name"] for t in assembly["tools"]}
        assert names == {"global-tool", "scoped-tool"}  # both contribute
        # the unscoped view never sees the scoped provider
        root_names = {t["name"] for t in prompt.assemble()["tools"]}
        assert root_names == {"global-tool"}

    def test_scoped_sections_shadow_global_nearest_wins(self) -> None:
        root_key = new_scope_key("root")
        leaf_key = new_scope_key("leaf")
        from AstralNexus.runtime.scope import bind_scope_parent
        bind_scope_parent(leaf_key, root_key)
        root_ctx = ScopedContext(object(), root_key)
        leaf_ctx = ScopedContext(root_ctx, leaf_key)
        prompt = SystemPrompt(config={"include_harness_identity": False})
        prompt.section({"name": "shared", "order": 5, "text": "global"})
        # register through scoped views by swapping the registry ctx
        prompt.ctx = root_ctx
        prompt.section({"name": "shared", "order": 5, "text": "root"})
        prompt.section({"name": "root-only", "order": 6, "text": "R"})
        prompt.ctx = leaf_ctx
        prompt.section({"name": "shared", "order": 5, "text": "leaf"})
        prompt.ctx = None
        assembly = prompt.assemble({"scope": leaf_key})
        texts = {section["name"]: section["text"] for section in assembly["sections"]}
        assert texts["shared"] == "leaf"  # nearest scope wins
        assert texts["root-only"] == "R"  # ancestors still contribute
        root_view = prompt.assemble({"scope": root_key})
        root_texts = {section["name"]: section["text"] for section in root_view["sections"]}
        assert root_texts["shared"] == "root"

    def test_variables_nearest_scope_shadows_global(self) -> None:
        key = new_scope_key("agent")
        scope = create_scope(object(), key)
        prompt = SystemPrompt(config={"include_harness_identity": False})
        prompt.variable("who", lambda ctx: "global")
        prompt.ctx = scope.ctx
        prompt.variable("who", lambda ctx: "scoped")
        prompt.ctx = None
        assert prompt.assemble({"scope": key})["variables"]["who"] == "scoped"
        assert prompt.assemble()["variables"]["who"] == "global"

    def test_multiple_complete_sections_throw_naming_them(self) -> None:
        prompt = SystemPrompt()
        prompt.section({"name": "c1", "order": 1, "text": "one", "complete": True})
        prompt.section({"name": "c2", "order": 2, "text": "two", "complete": True})
        with pytest.raises(ValueError, match='multiple complete prompt sections are active: "c1", "c2"'):
            prompt.assemble()

    def test_complete_section_restored_after_waterfall(self) -> None:
        prompt = SystemPrompt(config={"include_harness_identity": False})
        prompt.section({"name": "full", "order": 1, "text": "THE WHOLE PROMPT",
                        "complete": True})

        def interceptor(assembly, context):
            tampered = dict(assembly)
            tampered["sections"] = [{"name": "hijack", "text": "nope"}]
            return tampered

        prompt.on_assemble(interceptor)
        assembly = prompt.assemble()
        assert assembly["sections"] == [{"name": "full", "text": "THE WHOLE PROMPT"}]

    def test_interpolate_flag_survives_into_the_assembly(self) -> None:
        prompt = SystemPrompt(config={"include_harness_identity": False})
        prompt.section({"name": "raw", "order": 1, "text": "{{literal}}",
                        "interpolate": False})
        assembly = prompt.assemble()
        raw = next(s for s in assembly["sections"] if s["name"] == "raw")
        assert raw["interpolate"] is False
        assert render_prompt(assembly).startswith("{{literal}}")

    def test_suppression_from_any_chain_layer(self) -> None:
        root_key = new_scope_key("root")
        leaf_key = new_scope_key("leaf")
        from AstralNexus.runtime.scope import bind_scope_parent
        bind_scope_parent(leaf_key, root_key)
        prompt = SystemPrompt()
        prompt.context({"name": "c", "order": 1, "text": "visible"})
        assert len(prompt.assemble({"scope": leaf_key})["contexts"]) == 1
        root_ctx = ScopedContext(object(), root_key)
        prompt.ctx = root_ctx
        dispose_suppressor = prompt.suppress_runtime_context()
        prompt.ctx = None
        # suppression at an ancestor empties contexts for the descendant view
        assert prompt.assemble({"scope": leaf_key})["contexts"] == []
        dispose_suppressor()
        assert len(prompt.assemble({"scope": leaf_key})["contexts"]) == 1

    def test_multiple_suppressors_independently_disposable(self) -> None:
        prompt = SystemPrompt()
        prompt.context({"name": "c", "order": 1, "text": "t"})
        d1 = prompt.suppress_runtime_context()
        d2 = prompt.suppress_runtime_context()
        assert prompt.assemble()["contexts"] == []
        d1()
        assert prompt.assemble()["contexts"] == []  # d2 still suppresses
        d2()
        assert len(prompt.assemble()["contexts"]) == 1


class TestWaterfall:
    def test_no_interceptor_returns_the_assembly_itself(self) -> None:
        prompt = SystemPrompt()
        assembly = prompt.assemble()
        assert prompt.assemble() == assembly  # stable, fallback arm

    def test_fold_order_global_then_ancestors_then_exact(self) -> None:
        root_key = new_scope_key("root")
        leaf_key = new_scope_key("leaf")
        from AstralNexus.runtime.scope import bind_scope_parent
        bind_scope_parent(leaf_key, root_key)
        calls: list = []

        def make(tag):
            def handler(assembly, context):
                calls.append(tag)
                return assembly
            return handler

        prompt = SystemPrompt()
        prompt.on_assemble(make("global"))
        root_ctx = ScopedContext(object(), root_key)
        leaf_ctx = ScopedContext(root_ctx, leaf_key)
        prompt.on_assemble(make("leaf"), ctx=leaf_ctx)
        prompt.on_assemble(make("root"), ctx=root_ctx)
        prompt.assemble({"scope": leaf_key})
        assert calls == ["global", "root", "leaf"]  # farthest ancestor before exact scope

    def test_transformed_value_is_authoritative(self) -> None:
        prompt = SystemPrompt(config={"include_harness_identity": False})

        def interceptor(assembly, context):
            result = dict(assembly)
            result["variables"] = {**assembly["variables"], "injected": "yes"}
            return result

        dispose = prompt.on_assemble(interceptor)
        assert prompt.assemble()["variables"]["injected"] == "yes"
        dispose()
        assert "injected" not in prompt.assemble()["variables"]

    def test_handler_must_be_callable(self) -> None:
        prompt = SystemPrompt()
        with pytest.raises(TypeError, match="assemble handler must be callable"):
            prompt.on_assemble("not-callable")  # type: ignore[arg-type]


class TestOrderTables:
    def test_section_orders_are_canonical(self) -> None:
        assert SECTION_ORDERS["HARNESS_IDENTITY"] == -1000
        assert SECTION_ORDERS["DEPLOYMENT_PERSONA_PREFIX"] == 0
        assert SECTION_ORDERS["DEPLOYMENT_PERSONA_SUFFIX"] == 10200
        assert SECTION_ORDERS["TOOL_PWSH"] == 1010
        assert len(SECTION_ORDERS) == 32

    def test_context_orders_are_canonical(self) -> None:
        assert CONTEXT_ORDERS == {"SANDBOX_POLICY": 110, "APPROVAL_POLICY": 115,
                                 "SUBAGENT_DELEGATION": 120}

    def test_getters_read_the_tables(self) -> None:
        prompt = SystemPrompt()
        assert prompt.get_section_order("TOOL_READ") == 1100
        assert prompt.get_section_order("NOT_A_PLACEMENT") is None
        assert prompt.get_context_order("APPROVAL_POLICY") == 115
        assert prompt.get_context_order("nope") is None
