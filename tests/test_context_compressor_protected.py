"""Tests for #17 context compressor protected system prompt."""
from AstralNexus.runtime.context_compressor import ContextCompressor


class FakeSummaryClient:
    model = "fake-aux"

    def chat(self, messages, **kwargs):
        return "Preserved historical summary."


class TestProtectedSystemPrompt:
    def test_compressor_initializes_without_protected_prompt(self):
        compressor = ContextCompressor()
        assert compressor._protected_system_prompt is None

    def test_compressor_initializes_with_protected_prompt(self):
        compressor = ContextCompressor(protected_system_prompt="You are a helpful assistant.")
        assert compressor._protected_system_prompt == "You are a helpful assistant."

    def test_compress_preserves_system_prompt_in_head(self):
        """System prompt should always be in head, never compressed."""
        compressor = ContextCompressor(
            summary_client=FakeSummaryClient(),
            protected_system_prompt="You are a helpful assistant.",
            threshold=0.1,  # Force compression
        )
        
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "I'm good"},
            {"role": "user", "content": "Great"},
            {"role": "assistant", "content": "Thanks"},
        ]
        
        compacted, stats = compressor.compress(messages, context_length=100)
        
        # System prompt should be preserved
        if stats["compressed"]:
            assert compacted[0]["role"] == "system"
            assert "helpful assistant" in compacted[0]["content"]
