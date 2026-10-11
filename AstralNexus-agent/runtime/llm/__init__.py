"""Provider-neutral LLM service interface for the DeepSeek Harness.

Port of ``@deepseek-ai/dsh-llm`` (commit 5badb150; lib/index.js rollup of
the types modules plus lib/invariant.js). The aggregate export face mirrors
the upstream package export list (snake_case for functions, upstream
PascalCase retained for brand constructors and classes).

Layout:
- ``errors``: HarnessError base, canonical codes, context/quota classifiers,
  error_chain rendering.
- ``api_key``: the one well-formed-key definition.
- ``attribution``: mandatory product identity headers.
- ``brand``: the package-owned branded ids.
- ``call_config``: field-wise config equality and the agent-loop request
  mark.
- ``retry_policy``: provider-owned retry policy resolution.
- ``message``: immutable identified message constructors.
- ``adapter_failure``: adapter-boundary throw normalization.
- ``content``: content-block projection helpers (files, images, tool
  updates).
- ``assistant_stream``: the compact attempt-stream accumulator and readers.
- ``assembler``: the canonical chunk-to-message BlockAssembler.
- ``runtime``: LlmError, LlmAdapter, and the LlmRuntime service.
- ``invariant``: the llm-invariant companion (stream grammar enforcement).
"""
from __future__ import annotations

from .adapter_failure import normalize_llm_failure
from .api_key import LEGAL_API_KEY, normalize_api_key
from .assembler import BlockAssembler
from .assistant_stream import (AssistantStreamAccumulator,
                               assemble_assistant_stream,
                               assistant_stream_chunks,
                               assistant_stream_first_token_time,
                               assistant_stream_has_visible_content,
                               assistant_stream_has_visible_text,
                               chunk_has_visible_text,
                               expand_assistant_stream, is_token_delta,
                               is_visible_chunk, join_assistant_stream_text,
                               last_assistant_stream_chunk,
                               run_first_token_time, run_first_visible_time)
from .attribution import APP_IDENTITY, attribution_headers, user_agent
from .brand import (LlmAttemptId, MessageId, ProviderRequestId,
                    ReasoningEffortId, ToolCallId)
from .call_config import (call_config_equals, is_agent_loop_request,
                          mark_agent_loop_request)
from .content import (content_has_file, content_has_image, file_handle_text,
                      offloaded_image_text, project_files_to_text,
                      project_images_for_text_model,
                      project_offloaded_images, project_tool_updates,
                      request_image_handle_text, required_image_offload,
                      resolve_image_attachment_access, text_only_image_text)
from .errors import (ACCOUNT_QUOTA_EXCEEDED_CODE, CONTEXT_WINDOW_EXCEEDED_CODE,
                     EMPTY_RESPONSE_CODE, HarnessError,
                     IMAGE_OFFLOAD_REQUIRED_CODE, INVALID_CREDENTIAL_CODE,
                     QUOTA_EXCEEDED_CODE, error_chain, is_context_window_exceeded_error,
                     is_harness_error, is_quota_exceeded_error)
from .message import (CONTEXT_SUMMARY_MAX_CHARS, bound_context_summary,
                      create_assistant_message, create_developer_message,
                      create_message, create_system_message,
                      create_tool_result_message, create_user_message,
                      freeze_message)
from .retry_policy import (DEFAULT_INITIAL_DELAY_MS, DEFAULT_JITTER_RATIO,
                           DEFAULT_MAX_DELAY_MS, DEFAULT_MAX_RETRIES,
                           DEFAULT_RETRYABLE_CODES, RETRY_POLICY_SCHEMA,
                           resolve_retry_policy)
from .runtime import (LlmAdapter, LlmError, LlmRuntime, adapter_failure_chunk,
                      assert_usable_api_key)

__all__ = [
    "ACCOUNT_QUOTA_EXCEEDED_CODE",
    "APP_IDENTITY",
    "AssistantStreamAccumulator",
    "BlockAssembler",
    "CONTEXT_SUMMARY_MAX_CHARS",
    "CONTEXT_WINDOW_EXCEEDED_CODE",
    "DEFAULT_INITIAL_DELAY_MS",
    "DEFAULT_JITTER_RATIO",
    "DEFAULT_MAX_DELAY_MS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRYABLE_CODES",
    "EMPTY_RESPONSE_CODE",
    "HarnessError",
    "IMAGE_OFFLOAD_REQUIRED_CODE",
    "INVALID_CREDENTIAL_CODE",
    "LEGAL_API_KEY",
    "LlmAdapter",
    "LlmAttemptId",
    "LlmError",
    "LlmRuntime",
    "MessageId",
    "ProviderRequestId",
    "QUOTA_EXCEEDED_CODE",
    "RETRY_POLICY_SCHEMA",
    "ReasoningEffortId",
    "ToolCallId",
    "adapter_failure_chunk",
    "assemble_assistant_stream",
    "assert_usable_api_key",
    "assistant_stream_chunks",
    "assistant_stream_first_token_time",
    "assistant_stream_has_visible_content",
    "assistant_stream_has_visible_text",
    "attribution_headers",
    "bound_context_summary",
    "call_config_equals",
    "chunk_has_visible_text",
    "content_has_file",
    "content_has_image",
    "create_assistant_message",
    "create_developer_message",
    "create_message",
    "create_system_message",
    "create_tool_result_message",
    "create_user_message",
    "error_chain",
    "expand_assistant_stream",
    "file_handle_text",
    "freeze_message",
    "is_agent_loop_request",
    "is_context_window_exceeded_error",
    "is_harness_error",
    "is_quota_exceeded_error",
    "is_token_delta",
    "is_visible_chunk",
    "join_assistant_stream_text",
    "last_assistant_stream_chunk",
    "mark_agent_loop_request",
    "normalize_api_key",
    "normalize_llm_failure",
    "offloaded_image_text",
    "project_files_to_text",
    "project_images_for_text_model",
    "project_offloaded_images",
    "project_tool_updates",
    "request_image_handle_text",
    "required_image_offload",
    "resolve_image_attachment_access",
    "resolve_retry_policy",
    "run_first_token_time",
    "run_first_visible_time",
    "text_only_image_text",
    "user_agent",
]
