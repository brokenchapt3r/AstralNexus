"""Unified token estimation utilities.

Consolidates duplicate token counting logic from:
- runtime/context.py::estimate_tokens
- runtime/context_compressor.py::_rough_tokens
- core/reasoning_tier.py (uses base_budget multiplier, not duplication)

Usage:
    from AstralNexus.core.token_utils import estimate_tokens, rough_tokens_for_messages
    
    # Single text
    tokens = estimate_tokens("some text")
    
    # Message list (for context compression)
    tokens = rough_tokens_for_messages(messages)
"""
from typing import Any, Dict, List


def estimate_tokens(text: str) -> int:
    """Estimate token count using rough heuristic: ~4 chars per token.
    
    This is a simple approximation. For accurate counts, use provider
    tokenizers or the TokenCalibrator for runtime correction.
    
    Args:
        text: Input text to estimate.
        
    Returns:
        Estimated token count, minimum 1 for non-empty text, 0 for empty.
    """
    return max(1, len(text) // 4) if text else 0


def estimate_provider_input_tokens(char_count: int) -> int:
    """Use the conservative provider preflight estimate (three chars/token).

    Context compression and request admission must agree on this estimate;
    otherwise a prompt can appear below the compression threshold and then be
    measured one third larger by the provider-request boundary.
    """
    return max(0, (max(0, int(char_count)) + 2) // 3)


def rough_tokens_for_messages(messages: List[Dict[str, Any]]) -> int:
    """Estimate token count for a list of chat messages.
    
    Serializes messages to compact JSON and applies the ~4 chars/token
    heuristic. Used by ContextCompressor for pre-compression estimation.
    
    Args:
        messages: List of message dicts with 'role' and 'content' fields.
        
    Returns:
        Estimated token count.
    """
    import json
    text = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return estimate_provider_input_tokens(len(text))
