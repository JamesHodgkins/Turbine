"""Token Manager — counts tokens using mistral-common's Tekken tokenizer."""

from mistral_common.tokens.tokenizers.mistral import MistralTokenizer
from mistral_common.protocol.instruct.messages import UserMessage
from mistral_common.protocol.instruct.request import ChatCompletionRequest


class TokenManager:
    # Context limits per model (in tokens)
    MODEL_LIMITS: dict[str, int] = {
        "mistral-large-latest": 131072,
        "mistral-small-latest": 32768,
        "codestral-latest": 32768,
    }

    def __init__(self, model: str = "mistral-large-latest"):
        self.model = model
        self.limit = self.MODEL_LIMITS.get(model, 32768)
        # Load the Tekken tokenizer directly from its JSON file (no sentencepiece required)
        _data = MistralTokenizer._data_path()
        self._tokenizer = MistralTokenizer.from_file(str(_data / "tekken_240911.json"))

    def count(self, text: str) -> int:
        """Return the token count for a plain text string."""
        request = ChatCompletionRequest(
            messages=[UserMessage(content=text)],
            model=self.model,
        )
        tokenized = self._tokenizer.encode_chat_completion(request)
        return len(tokenized.tokens)

    def fits(self, text: str, reserve: int = 2048) -> bool:
        """Return True if text fits within the model's context window (minus reserve)."""
        return self.count(text) <= (self.limit - reserve)

    def truncate_to_fit(self, chunks: list[str], reserve: int = 2048) -> list[str]:
        """Given an ordered list of text chunks, return as many as fit within the limit."""
        budget = self.limit - reserve
        selected: list[str] = []
        used = 0
        for chunk in chunks:
            tokens = self.count(chunk)
            if used + tokens > budget:
                break
            selected.append(chunk)
            used += tokens
        return selected
