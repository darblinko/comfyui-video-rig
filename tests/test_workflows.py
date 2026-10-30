"""Structural checks on the committed workflow templates.

Pixel-level behaviour needs a running ComfyUI; these tests check the node
graph that produces it."""

import json

from conftest import REPO_DIR


def load_workflow(name: str) -> dict:
    raw = json.loads((REPO_DIR / "workflows" / name).read_text())
    return {node_id: node for node_id, node in raw.items() if not node_id.startswith("_")}


def node_by_title(workflow: dict, title: str) -> tuple[str, dict]:
    matches = [(node_id, node) for node_id, node in workflow.items() if node["_meta"]["title"] == title]
    assert len(matches) == 1, f"expected one {title} node"
    return matches[0]


def test_start_frame_composites_the_original_pixels_back_through_the_mask():
    workflow = load_workflow("flux_start_frame.json")
    start_id, _ = node_by_title(workflow, "RIG_START_IMAGE")
    mask_id, _ = node_by_title(workflow, "RIG_MASK")
    _, output = node_by_title(workflow, "RIG_OUTPUT")
    composite_id = output["inputs"]["images"][0]
    composite = workflow[composite_id]
    assert composite["class_type"] == "ImageCompositeMasked"
    assert composite["inputs"]["destination"] == [start_id, 0]
    assert composite["inputs"]["mask"] == [mask_id, 0]
    assert composite["inputs"]["resize_source"] is False
    assert (composite["inputs"]["x"], composite["inputs"]["y"]) == (0, 0)
    source_id = composite["inputs"]["source"][0]
    assert workflow[source_id]["class_type"] == "VAEDecode"
