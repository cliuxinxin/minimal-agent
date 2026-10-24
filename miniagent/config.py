"""Load the four standard provider settings."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .agent import Agent
from .builtin_tools import build_tools
from .model import AnthropicModel, OpenAIModel


class _Config(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    provider: Literal['openai', 'anthropic']
    model: str = Field(min_length=1)
    api_key: SecretStr
    base_url: str | None = None


def load_agent(path: str | Path = 'config.json', *, on_event=None, approve=None,
               workspace: str | Path = 'workspace') -> Agent:
    path = Path(path)
    if not path.is_file():
        raise ValueError('请复制 config.example.json 为 config.json，并填写模型和 API Key。')
    try:
        config = _Config.model_validate_json(path.read_text(encoding='utf-8'))
    except ValidationError as error:
        fields = ', '.join('.'.join(map(str, e['loc'])) or 'JSON'
                           for e in error.errors(include_input=False, include_url=False))
        raise ValueError(f'配置格式不正确，请检查：{fields}。只支持 provider、model、api_key、base_url。') from None
    if not config.model.strip() or not config.api_key.get_secret_value().strip():
        raise ValueError('model 和 api_key 不能为空。')
    model_type = OpenAIModel if config.provider == 'openai' else AnthropicModel
    model = model_type(config.model, config.api_key.get_secret_value(), config.base_url)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    return Agent(model, build_tools(workspace), on_event=on_event, approve=approve)
