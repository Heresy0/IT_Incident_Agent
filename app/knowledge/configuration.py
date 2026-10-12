"""Local index settings; none of these fields are model/tool arguments."""
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RunbookRAGConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    index_dir: str = Field(default='app/data/runbooks', min_length=1, max_length=1000)
    embedding_model: str = Field(default='text-embedding-v1', pattern=r'^[A-Za-z0-9_.-]{1,80}$')
    embedding_dimension: int = Field(default=1536, ge=2, le=4096)
    embedding_url: str = 'https://dashscope.aliyuncs.com'
    embedding_api_key_env: str = Field(default='DASHSCOPE_API_KEY', pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,79}$')
    top_k: int = Field(default=3, ge=1, le=4)
    min_score: float = Field(default=0.35, ge=-1, le=1)
    chunk_size: int = Field(default=500, ge=100, le=1000)
    chunk_overlap: int = Field(default=50, ge=0, le=200)

    @field_validator('embedding_url')
    @classmethod
    def endpoint(cls, value):
        from providers.http import validate_endpoint
        from providers.fixtures import ProviderError
        try:
            validate_endpoint(value)
        except ProviderError as exc:
            raise ValueError(exc.code) from None
        return value

    @model_validator(mode='after')
    def overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError('CHUNK_OVERLAP_INVALID')
        return self
