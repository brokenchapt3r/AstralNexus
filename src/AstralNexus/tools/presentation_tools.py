"""Lead-only presentation tool; registration is not an acceptance decision."""
import json
from AstralNexus.tools.registry import registry
from AstralNexus.runtime.presentation import current_presentation


def present_files(args, **kwargs):
    context = current_presentation()
    if context is None:
        return json.dumps({"status": "error", "error": "present_files is available only to the delivery lead"})
    try:
        return json.dumps(context.present(args.get("paths")), ensure_ascii=False)
    except (ValueError, OSError) as error:
        return json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False)


registry.register(name="present_files", toolset="delivery", schema={"name": "present_files",
    "description": "Present files or directories inside outputs to the user. This records paths, not content acceptance.",
    "parameters": {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]}},
    handler=present_files)
