import os
import sys
import importlib.util
from unittest.mock import patch


_spec = importlib.util.spec_from_file_location(
    "mcp_server_plugin_test",
    os.path.join(os.path.dirname(__file__), "..", "mcp_server.py"),
)
mcp_server = importlib.util.module_from_spec(_spec)
sys.modules["mcp_server_plugin_test"] = mcp_server
_spec.loader.exec_module(mcp_server)


def test_make_plugin_request_prefers_neutral_endpoint():
    with patch.object(mcp_server, "make_request", return_value={"workflow": "ok"}) as mock_make_request:
        result = mcp_server.make_plugin_request("workflow")

    assert result == {"workflow": "ok"}
    mock_make_request.assert_called_once_with("/comfy-pilot/workflow", method="GET", data=None, timeout=None)


def test_make_plugin_request_falls_back_to_legacy_endpoint_on_404():
    with patch.object(
        mcp_server,
        "make_request",
        side_effect=[
            {"error": "HTTP error from ComfyUI: 404 Not Found"},
            {"workflow": "legacy"},
        ],
    ) as mock_make_request:
        result = mcp_server.make_plugin_request("workflow")

    assert result == {"workflow": "legacy"}
    assert mock_make_request.call_count == 2
    assert mock_make_request.call_args_list[1].args[0] == "/claude-code/workflow"


def test_make_plugin_request_adds_client_id_to_get_requests():
    with patch.object(mcp_server, "make_request", return_value={"workflow": "ok"}) as mock_make_request:
        result = mcp_server.make_plugin_request("workflow", client_id="page-123")

    assert result == {"workflow": "ok"}
    mock_make_request.assert_called_once_with(
        "/comfy-pilot/workflow?client_id=page-123",
        method="GET",
        data=None,
        timeout=None,
    )


def test_make_plugin_request_adds_client_id_to_post_body():
    with patch.object(mcp_server, "make_request", return_value={"status": "ok"}) as mock_make_request:
        result = mcp_server.make_plugin_request(
            "graph_command",
            method="POST",
            data={"action": "queue_prompt", "params": {}},
            client_id="page-123",
        )

    assert result == {"status": "ok"}
    mock_make_request.assert_called_once_with(
        "/comfy-pilot/graph-command",
        method="POST",
        data={"action": "queue_prompt", "params": {}, "client_id": "page-123"},
        timeout=None,
    )


def test_send_graph_command_uses_client_id_from_environment():
    with (
        patch.dict(os.environ, {mcp_server.WORKFLOW_CLIENT_ENV_VAR: "page-456"}, clear=False),
        patch.object(mcp_server, "make_request", return_value={"status": "ok"}) as mock_make_request,
    ):
        result = mcp_server.send_graph_command("queue_prompt", {})

    assert result == {"status": "ok"}
    mock_make_request.assert_called_once_with(
        "/comfy-pilot/graph-command",
        method="POST",
        data={"action": "queue_prompt", "params": {}, "client_id": "page-456"},
        timeout=None,
    )


def test_get_workflow_does_not_fall_back_to_history_for_explicit_missing_client():
    with patch.object(
        mcp_server,
        "make_request",
        return_value={"error": "HTTP error from ComfyUI: 404 Not Found"},
    ):
        result = mcp_server.get_workflow(client_id="missing-client")

    assert result == {
        "message": (
            "No workflow found for client_id 'missing-client'. "
            "Use list_workflow_clients to discover available targets."
        )
    }


def test_open_subgraph_supports_locator_node_ids():
    with patch.object(mcp_server, "send_graph_command", return_value={"status": "opened"}) as mock_send_graph_command:
        result = mcp_server.open_subgraph(node_id="subgraph-123:44")

    assert result == {"status": "opened"}
    mock_send_graph_command.assert_called_once_with(
        "open_subgraph",
        {"graph_id": "subgraph-123", "node_id": "44"},
    )


def test_close_subgraph_supports_all_levels_flag():
    with patch.object(mcp_server, "send_graph_command", return_value={"status": "closed"}) as mock_send_graph_command:
        result = mcp_server.close_subgraph(all_levels=True)

    assert result == {"status": "closed"}
    mock_send_graph_command.assert_called_once_with(
        "close_subgraph",
        {"all_levels": True},
    )


def test_tools_list_includes_edit_subgraph():
    response = mcp_server.handle_request({"method": "tools/list", "id": 1})

    tools = response["result"]["tools"]
    edit_subgraph_tool = next(tool for tool in tools if tool["name"] == "edit_subgraph")

    assert edit_subgraph_tool["inputSchema"]["required"] == ["graph_id", "operations"]
    assert "graph_id" in edit_subgraph_tool["inputSchema"]["properties"]


def test_tools_call_dispatches_edit_subgraph():
    with patch.object(mcp_server, "edit_subgraph", return_value="ok: 1/1") as mock_edit_subgraph:
        response = mcp_server.handle_request({
            "method": "tools/call",
            "id": 2,
            "params": {
                "name": "edit_subgraph",
                "arguments": {
                    "graph_id": "subgraph-123",
                    "operations": [{"action": "create", "node_type": "KSampler"}],
                },
            },
        })

    assert response["result"]["content"][0]["text"] == "ok: 1/1"
    mock_edit_subgraph.assert_called_once_with("subgraph-123", [{"action": "create", "node_type": "KSampler"}])
