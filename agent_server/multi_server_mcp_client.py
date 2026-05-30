from datetime import timedelta
from typing import Any, Callable, Union

import httpx
from databricks.sdk import WorkspaceClient
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from pydantic import BaseModel, ConfigDict, Field


class WorkspaceClientAuth(httpx.Auth):
    """httpx auth that injects fresh credentials from a WorkspaceClient on every request."""

    requires_request_body = False
    requires_response_body = False

    def __init__(self, workspace_client: WorkspaceClient):
        self.workspace_client = workspace_client

    def auth_flow(self, request):
        for key, value in self.workspace_client.config.authenticate().items():
            request.headers[key] = value
        yield request


class MCPServer(BaseModel):
    """
    Base configuration for an MCP server connection using streamable HTTP transport.

    Accepts any additional keyword arguments which are automatically passed through
    to LangChain's Connection type, making this forward-compatible with future updates.

    Common optional parameters:
        - headers: dict[str, str] - Custom HTTP headers
        - timeout: float | timedelta - Request timeout in seconds
        - sse_read_timeout: float - SSE read timeout in seconds
        - auth: httpx.Auth - Authentication handler
        - httpx_client_factory: Callable - Custom httpx client factory
        - terminate_on_close: bool - Terminate connection on close
        - session_kwargs: dict - Additional session kwargs
        - handle_tool_error: bool | str | Callable - Error handling strategy
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    name: str = Field(..., exclude=True, description="Name to identify this server connection")
    url: str
    handle_tool_error: Union[bool, str, Callable[[Exception], str], None] = Field(
        default=None,
        exclude=True,
        description=(
            "If True, return the error message as the output. If False, raise the error. "
            "If a string, return the string as the error message. "
            "If a callable, return the result of the callable as the error message."
        ),
    )
    headers: dict[str, str] | None = Field(
        default=None, description="HTTP headers to send to the endpoint."
    )
    timeout: float | timedelta | None = Field(default=None, description="HTTP timeout.")

    def to_connection_dict(self) -> StreamableHttpConnection:
        """Convert to connection dictionary for LangChain MultiServerMCPClient."""
        data = self.model_dump()
        data["transport"] = "streamable_http"
        if isinstance(data["timeout"], float):
            data["timeout"] = timedelta(seconds=data["timeout"])
        return data


class DatabricksMCPServer(MCPServer):
    """
    MCP server configuration with authentication taken directly from a passed-in
    WorkspaceClient. Unlike the upstream `databricks-ai-bridge` version, this uses
    a simple httpx.Auth wrapper around `workspace_client.config.authenticate()`
    instead of the OAuth client provider — credentials are refreshed on every
    request via the workspace client's existing auth flow.
    """

    workspace_client: WorkspaceClient | None = Field(
        default=None,
        description="Databricks WorkspaceClient used for authentication. If None, will be auto-initialized.",
        exclude=True,
    )

    @classmethod
    def from_uc_function(
        cls,
        catalog: str,
        schema: str,
        name: str,
        function_name: str | None = None,
        workspace_client: WorkspaceClient | None = None,
        **kwargs,
    ) -> "DatabricksMCPServer":
        """Create a Databricks MCP server from a Unity Catalog function path."""
        ws_client = workspace_client or WorkspaceClient()
        base_url = ws_client.config.host

        if function_name:
            url = f"{base_url}/api/2.0/mcp/functions/{catalog}/{schema}/{function_name}"
        else:
            url = f"{base_url}/api/2.0/mcp/functions/{catalog}/{schema}"

        return cls(name=name, url=url, workspace_client=ws_client, **kwargs)

    @classmethod
    def from_vector_search(
        cls,
        catalog: str,
        schema: str,
        name: str,
        index_name: str | None = None,
        workspace_client: WorkspaceClient | None = None,
        **kwargs,
    ) -> "DatabricksMCPServer":
        """Create a Databricks MCP server from a Unity Catalog vector search index path."""
        ws_client = workspace_client or WorkspaceClient()
        base_url = ws_client.config.host

        if index_name:
            url = f"{base_url}/api/2.0/mcp/vector-search/{catalog}/{schema}/{index_name}"
        else:
            url = f"{base_url}/api/2.0/mcp/vector-search/{catalog}/{schema}"

        return cls(name=name, url=url, workspace_client=ws_client, **kwargs)

    def model_post_init(self, context: Any) -> None:
        super().model_post_init(context)
        if self.workspace_client is None:
            self.workspace_client = WorkspaceClient()
        self._auth = WorkspaceClientAuth(self.workspace_client)

    def to_connection_dict(self) -> StreamableHttpConnection:
        data = super().to_connection_dict()
        data["auth"] = self._auth
        return data


# Fields that langchain_mcp_adapters adds to content blocks (e.g., auto-generated
# `id` and `index` from `create_text_block`) but that Anthropic's tool_result
# content schema rejects. Strip them before the blocks reach the model.
_ANTHROPIC_REJECTED_BLOCK_FIELDS = ("id", "index")


def _sanitize_content_blocks(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    cleaned: list[Any] = []
    for block in content:
        if isinstance(block, dict):
            cleaned.append(
                {k: v for k, v in block.items() if k not in _ANTHROPIC_REJECTED_BLOCK_FIELDS}
            )
        else:
            cleaned.append(block)
    return cleaned


def _wrap_tool_coroutine(tool: Any) -> Any:
    original = tool.coroutine
    if original is None:
        return tool

    async def wrapped(**kwargs):
        result = await original(**kwargs)
        # response_format="content_and_artifact" -> (content, artifact)
        if isinstance(result, tuple) and len(result) == 2:
            content, artifact = result
            return _sanitize_content_blocks(content), artifact
        return _sanitize_content_blocks(result)

    tool.coroutine = wrapped
    return tool


class DatabricksMultiServerMCPClient(MultiServerMCPClient):
    """
    MultiServerMCPClient with simplified configuration for Databricks servers.

    Authentication is taken from the WorkspaceClient passed to each
    `DatabricksMCPServer` — no OAuth provider, no custom httpx client factory.
    """

    def __init__(self, servers: list[MCPServer], **kwargs):
        self._server_configs = {server.name: server for server in servers}

        connections: dict[str, StreamableHttpConnection] = {
            server.name: server.to_connection_dict() for server in servers
        }
        super().__init__(connections=connections, **kwargs)  # type: ignore[arg-type]

    async def get_tools(self, server_name: str | None = None):
        """Get tools from MCP servers, applying handle_tool_error configuration."""
        import asyncio

        server_names = [server_name] if server_name is not None else list(self.connections.keys())

        load_tool_tasks = [
            asyncio.create_task(
                super(DatabricksMultiServerMCPClient, self).get_tools(server_name=name)
            )
            for name in server_names
        ]
        tools_list = await asyncio.gather(*load_tool_tasks)

        all_tools = []
        for name, tools in zip(server_names, tools_list, strict=True):
            if name in self._server_configs:
                server_config = self._server_configs[name]
                if server_config.handle_tool_error is not None:
                    for tool in tools:
                        tool.handle_tool_error = server_config.handle_tool_error
            for tool in tools:
                _wrap_tool_coroutine(tool)
            all_tools.extend(tools)

        return all_tools
