import os

from openai import OpenAI


def is_configured():
    return bool(os.environ.get("OPENAI_API_KEY"))


def transcribe(audio_file):
    """Transcribe an uploaded audio file (a Flask FileStorage) via OpenAI's Whisper API."""
    client = OpenAI()
    response = client.audio.transcriptions.create(
        model="whisper-1",
        file=(audio_file.filename or "recording.webm", audio_file.read(), audio_file.mimetype or "audio/webm"),
    )
    return response.text
