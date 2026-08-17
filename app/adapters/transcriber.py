"""Audio transcription port with OpenAI Whisper and a deterministic fake."""

from typing import Protocol

from openai import AsyncOpenAI


class Transcriber(Protocol):
    """Convert an inbound voice note to text."""

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        """Return the transcript for one audio file."""


class OpenAIWhisper:
    """OpenAI audio transcription adapter pinned to the requested whisper-1 model."""

    def __init__(self, api_key: str, client: AsyncOpenAI | None = None) -> None:
        self._client = client or AsyncOpenAI(api_key=api_key)

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        result = await self._client.audio.transcriptions.create(
            model="whisper-1", file=(filename, audio, content_type)
        )
        return result.text.strip()


class FakeTranscriber:
    """Return a preconfigured transcript and capture submitted audio."""

    def __init__(self, transcript: str = "book appointment") -> None:
        self.transcript = transcript
        self.calls: list[tuple[bytes, str, str]] = []

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        self.calls.append((audio, filename, content_type))
        return self.transcript

