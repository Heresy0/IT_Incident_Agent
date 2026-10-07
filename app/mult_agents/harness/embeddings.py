"""Reuse DashScope embeddings with bounded retries and a timed call boundary."""
from contextlib import nullcontext

from langchain_community.embeddings import DashScopeEmbeddings

from .runtime import current


class TimedEmbeddingClient:
    def __init__(self, client):
        self.client = client

    def call(self, **kwargs):
        context = current()
        if context:
            context.reserve("embedding")
        with context.span("embedding", "text_embedding") if context else nullcontext():
            return self.client.call(**kwargs, request_timeout=30)


def build_embeddings(model, api_key):
    embeddings = DashScopeEmbeddings(model=model, dashscope_api_key=api_key, max_retries=1)
    embeddings.client = TimedEmbeddingClient(embeddings.client)
    return embeddings
