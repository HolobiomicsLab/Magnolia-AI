"""Workflow template tools: load, validate, and track multi-step workflows."""

import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


def load_workflow(template_path: str) -> dict[str, Any]:
    """Parse a YAML workflow template, validate it, return the plan.

    The template must have 'name' and 'steps' keys. Each step must have
    'name' and 'tool', and may have 'gate', 'outputs', and 'depends_on'.
    """
    if yaml is None:
        return {"success": False, "error": "PyYAML is required: pip install pyyaml"}

    tpl = Path(template_path)
    if not tpl.exists():
        return {"success": False, "error": f"Template not found: {template_path}"}

    try:
        with open(tpl) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return {"success": False, "error": f"YAML parse error: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    if not isinstance(data, dict):
        return {"success": False, "error": "Template must be a YAML mapping"}

    # Validate required top-level keys
    if "name" not in data:
        return {"success": False, "error": "Template missing required key: 'name'"}
    if "steps" not in data:
        return {"success": False, "error": "Template missing required key: 'steps'"}

    steps = data["steps"]
    if not isinstance(steps, list) or len(steps) == 0:
        return {"success": False, "error": "'steps' must be a non-empty list"}

    step_names = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            return {"success": False, "error": f"Step {i} must be a mapping"}
        if "name" not in step:
            return {"success": False, "error": f"Step {i} missing required key: 'name'"}
        if "tool" not in step:
            return {"success": False, "error": f"Step {i} missing required key: 'tool'"}
        if step["name"] in step_names:
            return {
                "success": False,
                "error": f"Duplicate step name: {step['name']}",
            }
        step_names.add(step["name"])

    # Validate depends_on references
    for i, step in enumerate(steps):
        for dep in step.get("depends_on", []):
            if dep not in step_names:
                return {
                    "success": False,
                    "error": f"Step '{step['name']}' depends on unknown step '{dep}'",
                }

    return {
        "success": True,
        "name": data["name"],
        "description": data.get("description", ""),
        "steps": steps,
        "template_path": str(tpl),
    }


def get_workflow_status(run_dir: str) -> dict[str, Any]:
    """Report which steps are complete based on output files in a run directory.

    Reads a workflow_plan.yaml from the run directory to determine the steps,
    then checks for each step's declared outputs.
    """
    rdir = Path(run_dir)
    if not rdir.exists():
        return {"success": False, "error": f"Run directory not found: {run_dir}"}

    plan_file = rdir / "workflow_plan.yaml"
    if not plan_file.exists():
        # Also check for the plan stored as JSON
        plan_file = rdir / "workflow_plan.json"
        if not plan_file.exists():
            return {
                "success": False,
                "error": "No workflow_plan.yaml or workflow_plan.json found in run directory",
            }

    # Load the plan
    plan_result = load_workflow(str(plan_file))
    if not plan_result.get("success"):
        # Try JSON fallback
        try:
            import json
            data = json.loads(plan_file.read_text())
            steps = data.get("steps", [])
            plan_name = data.get("name", "unknown")
        except Exception:
            return plan_result
    else:
        steps = plan_result["steps"]
        plan_name = plan_result["name"]

    step_status = []
    for step in steps:
        step_info: dict[str, Any] = {
            "name": step["name"],
            "tool": step["tool"],
            "complete": True,
            "outputs_found": [],
            "outputs_missing": [],
        }

        outputs = step.get("outputs", [])
        for out in outputs:
            # Check if the output exists (file or directory)
            out_path = rdir / step["name"] / out
            if out_path.exists() or (rdir / out).exists():
                step_info["outputs_found"].append(out)
            else:
                step_info["complete"] = False
                step_info["outputs_missing"].append(out)

        if not outputs:
            # No outputs declared, cannot determine completion
            step_info["complete"] = None

        step_status.append(step_info)

    completed_count = sum(1 for s in step_status if s["complete"] is True)
    total_count = len(step_status)

    return {
        "success": True,
        "workflow": plan_name,
        "run_dir": str(rdir),
        "total_steps": total_count,
        "completed_steps": completed_count,
        "all_complete": completed_count == total_count,
        "steps": step_status,
    }
