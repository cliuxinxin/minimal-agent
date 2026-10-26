"""Load standard provider settings and optional local agent capabilities."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .agent import Agent
from .builtin_tools import build_tools
from .model import AnthropicModel, OpenAIModel
from .programming import ProgrammingTools
from .search import build_search_tools


class _Config(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    provider: Literal['openai', 'anthropic']
    model: str = Field(min_length=1)
    api_key: SecretStr
    base_url: str | None = None
    max_steps: int | None = Field(default=80, ge=1)
    search_api_key: SecretStr | None = None


def load_agent(path: str | Path = 'config.json', *, on_event=None, approve=None,
               workspace: str | Path = 'workspace', stream: bool = False) -> Agent:
    path = Path(path)
    if not path.is_file():
        raise ValueError('请复制 config.example.json 为 config.json，并填写模型和 API Key。')
    try:
        config = _Config.model_validate_json(path.read_text(encoding='utf-8'))
    except ValidationError as error:
        fields = ', '.join('.'.join(map(str, e['loc'])) or 'JSON'
                           for e in error.errors(include_input=False, include_url=False))
        raise ValueError(f'配置格式不正确，请检查：{fields}。') from None
    return build_agent(config, root=path.resolve().parent, on_event=on_event,
                       approve=approve, workspace=workspace, stream=stream)


def build_agent(config: _Config, *, root: Path, on_event=None, approve=None,
                workspace: str | Path = 'workspace', stream: bool = False) -> Agent:
    """Build from validated settings without writing credentials to a second file."""
    if not config.model.strip() or not config.api_key.get_secret_value().strip():
        raise ValueError('model 和 api_key 不能为空。')
    model_type = OpenAIModel if config.provider == 'openai' else AnthropicModel
    model = model_type(config.model, config.api_key.get_secret_value(), config.base_url)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    programming = ProgrammingTools(Path(root).resolve(), workspace.resolve())
    tools = build_tools(workspace) + programming.builtin_tools()
    if config.search_api_key and config.search_api_key.get_secret_value().strip():
        tools.extend(build_search_tools(config.search_api_key.get_secret_value()))
    return Agent(model, tools, max_steps=config.max_steps, on_event=on_event,
                 approve=programming.approve if approve is None else approve,
                 dynamic_tools=programming.load_tools, stream=stream, planning=True)
