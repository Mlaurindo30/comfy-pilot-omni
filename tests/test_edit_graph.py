"""Tests for edit_graph() input parsing, validation, and operation handling."""

import importlib.util
import json
import os
import pytest
from unittest.mock import patch

# Load mcp_server.py directly to avoid importing the root __init__.py (ComfyUI plugin)
import sys
_spec = importlib.util.spec_from_file_location(
    "mcp_server",
    os.path.join(os.path.dirname(__file__), "..", "mcp_server.py"),
)
mcp_server = importlib.util.module_from_spec(_spec)
sys.modules["mcp_server"] = mcp_server
_spec.loader.exec_module(mcp_server)
edit_graph = mcp_server.edit_graph
get_node_info = mcp_server.get_node_info


SUBGRAPH_ID = "a3c0dab6-b250-4585-a0f9-8fb8b074fb2f"
OTHER_SUBGRAPH_ID = "bbbbbbbb-b250-4585-a0f9-8fb8b074fb2f"


@pytest.fixture
def mock_comfyui():
    """Patch all network-dependent functions used by edit_graph."""
    with patch("mcp_server.get_object_info_cached") as mock_info, \
         patch("mcp_server.send_graph_command") as mock_cmd, \
         patch("mcp_server.get_workflow") as mock_wf:

        mock_info.return_value = {"KSampler": {}, "CLIPTextEncode": {}}
        mock_cmd.return_value = {"node_id": "1", "size": [300, 100]}
        mock_wf.return_value = {"workflow": {"nodes": []}}

        yield {
            "info": mock_info,
            "cmd": mock_cmd,
            "wf": mock_wf,
        }


# --- Input parsing (PR #4 fix) ---

class TestEditGraphInputParsing:
    """Tests for the JSON string parsing fix from PR #4."""

    def test_json_string_list(self, mock_comfyui):
        ops = json.dumps([{"action": "create", "node_type": "KSampler"}])
        result = edit_graph(ops)
        assert "ok: 1/1" in result
        mock_comfyui["cmd"].assert_called_once()

    def test_json_string_single_object(self, mock_comfyui):
        ops = json.dumps({"action": "create", "node_type": "KSampler"})
        result = edit_graph(ops)
        assert "ok: 1/1" in result

    def test_normal_list(self, mock_comfyui):
        result = edit_graph([{"action": "create", "node_type": "KSampler"}])
        assert "ok: 1/1" in result

    def test_normal_dict(self, mock_comfyui):
        result = edit_graph({"action": "create", "node_type": "KSampler"})
        assert "ok: 1/1" in result

    def test_invalid_json_string(self, mock_comfyui):
        result = edit_graph("not valid json")
        assert "error:" in result
        assert "Invalid operations" in result
        mock_comfyui["cmd"].assert_not_called()

    def test_json_primitive_int(self, mock_comfyui):
        result = edit_graph("42")
        assert "error:" in result
        assert "Invalid operations" in result

    def test_json_primitive_null(self, mock_comfyui):
        result = edit_graph("null")
        assert "error:" in result
        assert "Invalid operations" in result

    def test_json_primitive_bool(self, mock_comfyui):
        result = edit_graph("true")
        assert "error:" in result
        assert "Invalid operations" in result

    def test_double_encoded_string(self, mock_comfyui):
        inner = json.dumps([{"action": "create", "node_type": "KSampler"}])
        double_encoded = json.dumps(inner)  # string wrapping a string
        result = edit_graph(double_encoded)
        assert "error:" in result
        assert "Invalid operations" in result


# --- Operation validation ---

class TestEditGraphOperations:

    def test_empty_list(self, mock_comfyui):
        result = edit_graph([])
        assert "ok: 0/0" in result
        mock_comfyui["cmd"].assert_not_called()

    def test_unknown_node_type(self, mock_comfyui):
        result = edit_graph([{"action": "create", "node_type": "DoesNotExist"}])
        assert "failed:" in result
        assert "Unknown node type" in result

    def test_create_missing_node_type(self, mock_comfyui):
        result = edit_graph([{"action": "create"}])
        assert "failed:" in result
        assert "node_type is required" in result

    def test_unknown_action(self, mock_comfyui):
        result = edit_graph([{"action": "foo"}])
        assert "failed:" in result
        assert "Unknown action: foo" in result

    def test_get_object_info_error(self, mock_comfyui):
        mock_comfyui["info"].return_value = {"error": "Connection refused"}
        result = edit_graph([{"action": "create", "node_type": "KSampler"}])
        assert "error:" in result
        assert "Connection refused" in result

    def test_create_with_ref_resolution(self, mock_comfyui):
        mock_comfyui["cmd"].side_effect = [
            {"node_id": "10", "size": [300, 100]},
            {"node_id": "11", "size": [300, 100]},
            {"status": "ok"},
        ]
        result = edit_graph([
            {"action": "create", "node_type": "KSampler", "ref": "sampler"},
            {"action": "create", "node_type": "CLIPTextEncode", "ref": "clip"},
            {"action": "connect", "from_node": "clip", "from_slot": 0, "to_node": "sampler", "to_slot": 1},
        ])
        assert "ok: 3/3" in result
        # Verify connect was called with resolved node IDs, not refs
        connect_call = mock_comfyui["cmd"].call_args_list[2]
        assert connect_call[0][1]["from_node_id"] == "11"
        assert connect_call[0][1]["to_node_id"] == "10"

    def test_mixed_success_and_failure(self, mock_comfyui):
        result = edit_graph([
            {"action": "create", "node_type": "KSampler"},
            {"action": "create", "node_type": "DoesNotExist"},
        ])
        assert "failed: 1/2" in result

    def test_set_single_property(self, mock_comfyui):
        mock_comfyui["cmd"].return_value = {"status": "ok"}
        result = edit_graph([{"action": "set", "node_id": "1", "property": "steps", "value": 30}])
        assert "ok: 1/1" in result
        mock_comfyui["cmd"].assert_called_once_with("set_node_property", {
            "graph_id": None,
            "node_id": "1",
            "property_name": "steps",
            "value": 30,
        })

    def test_set_multiple_properties(self, mock_comfyui):
        mock_comfyui["cmd"].return_value = {"status": "ok"}
        result = edit_graph([{"action": "set", "node_id": "1", "properties": {"steps": 30, "cfg": 7.5}}])
        assert "ok: 1/1" in result
        assert mock_comfyui["cmd"].call_count == 2

    def test_set_missing_node_id(self, mock_comfyui):
        result = edit_graph([{"action": "set", "property": "steps", "value": 30}])
        assert "failed:" in result
        assert "node_id is required" in result

    def test_connect_missing_nodes(self, mock_comfyui):
        result = edit_graph([{"action": "connect", "from_node": "1"}])
        assert "failed:" in result
        assert "from_node and to_node are required" in result


class TestEditGraphSubgraphs:

    def test_create_in_subgraph_passes_graph_id(self, mock_comfyui):
        mock_comfyui["cmd"].return_value = {"node_id": "61", "graph_id": SUBGRAPH_ID, "size": [300, 100]}

        result = edit_graph([{"action": "create", "node_type": "KSampler", "graph_id": SUBGRAPH_ID}])

        assert "ok: 1/1" in result
        mock_comfyui["cmd"].assert_called_once_with("create_node", {
            "type": "KSampler",
            "pos_x": 100,
            "pos_y": 100,
            "title": None,
            "graph_id": SUBGRAPH_ID,
            "place_in_view": False,
            "viewport_offset": 0,
        })

    def test_set_locator_node_passes_graph_id(self, mock_comfyui):
        mock_comfyui["cmd"].return_value = {"status": "ok", "graph_id": SUBGRAPH_ID}

        result = edit_graph([{"action": "set", "node_id": f"{SUBGRAPH_ID}:19", "property": "cfg", "value": 1.0}])

        assert "ok: 1/1" in result
        mock_comfyui["cmd"].assert_called_once_with("set_node_property", {
            "graph_id": SUBGRAPH_ID,
            "node_id": "19",
            "property_name": "cfg",
            "value": 1.0,
        })

    def test_connect_locator_nodes_in_same_subgraph(self, mock_comfyui):
        mock_comfyui["cmd"].return_value = {"status": "ok", "graph_id": SUBGRAPH_ID}

        result = edit_graph([{
            "action": "connect",
            "from_node": f"{SUBGRAPH_ID}:45",
            "from_slot": 0,
            "to_node": f"{SUBGRAPH_ID}:11",
            "to_slot": 0,
        }])

        assert "ok: 1/1" in result
        mock_comfyui["cmd"].assert_called_once_with("connect_nodes", {
            "graph_id": SUBGRAPH_ID,
            "from_node_id": "45",
            "from_slot": 0,
            "to_node_id": "11",
            "to_slot": 0,
        })

    def test_connect_cross_graph_nodes_rejected(self, mock_comfyui):
        result = edit_graph([{
            "action": "connect",
            "from_node": f"{SUBGRAPH_ID}:45",
            "to_node": f"{OTHER_SUBGRAPH_ID}:11",
        }])

        assert "failed: 1/1" in result
        assert "must be in the same graph" in result
        mock_comfyui["cmd"].assert_not_called()

    def test_created_ref_keeps_subgraph_context(self, mock_comfyui):
        mock_comfyui["cmd"].side_effect = [
            {"node_id": "61", "graph_id": SUBGRAPH_ID, "size": [300, 100]},
            {"status": "ok", "graph_id": SUBGRAPH_ID},
        ]

        result = edit_graph([
            {"action": "create", "node_type": "KSampler", "graph_id": SUBGRAPH_ID, "ref": "sampler"},
            {"action": "set", "node_id": "sampler", "property": "cfg", "value": 1.0},
        ])

        assert "ok: 2/2" in result
        second_call = mock_comfyui["cmd"].call_args_list[1]
        assert second_call[0][1]["graph_id"] == SUBGRAPH_ID
        assert second_call[0][1]["node_id"] == "61"


class TestGetNodeInfoSubgraphs:

    def test_get_node_info_supports_nested_locator(self):
        workflow = {
            "workflow": {
                "id": "root-workflow",
                "nodes": [{"id": 60, "type": SUBGRAPH_ID}],
                "definitions": {
                    "subgraphs": [
                        {
                            "id": SUBGRAPH_ID,
                            "nodes": [
                                {
                                    "id": 19,
                                    "type": "KSampler",
                                    "pos": [10, 20],
                                    "size": [300, 620],
                                    "inputs": [{"name": "model", "link": 79}],
                                    "outputs": [{"name": "LATENT", "links": [10]}],
                                    "widgets_values": [123, "randomize", 8, 1.0],
                                }
                            ],
                        }
                    ]
                },
            }
        }

        with patch("mcp_server.get_workflow", return_value=workflow), \
             patch("mcp_server.get_object_info_cached", return_value={
                 "KSampler": {
                     "category": "sampling",
                     "description": "Sampler node",
                     "input": {"required": {"model": ["MODEL"], "steps": ["INT"], "cfg": ["FLOAT"]}},
                     "output": ["LATENT"],
                     "output_name": ["LATENT"],
                 }
             }):
            result = get_node_info(f"{SUBGRAPH_ID}:19")

        assert f"locator: {SUBGRAPH_ID}:19" in result
        assert f"graph: {SUBGRAPH_ID}" in result
        assert "type: KSampler" in result
