"""Pure summaries of explicit plan metadata, never scheduling or duration promises."""


def presentation_metadata(nodes: list[dict]) -> dict:
    """Summarize validated, wave-labelled nodes without inferring missing estimates.

    A wave range assumes every task starts together with enough capacity.
    Cycles label saved DAG nodes only; they do not introduce execution loops.
    """
    waves = {}
    cycles = {}
    for node in nodes:
        waves.setdefault(node["wave"], []).append(node)
        if cycle := node.get("cycle"):
            group = cycles.setdefault(
                cycle["id"],
                {
                    "id": cycle["id"],
                    "label": cycle["label"],
                    "max_iterations": cycle["max_iterations"],
                    "iterations": [],
                    "node_ids": [],
                },
            )
            if cycle["iteration"] not in group["iterations"]:
                group["iterations"].append(cycle["iteration"])
            group["node_ids"].append(node["id"])
    summaries = []
    for wave, members in sorted(waves.items()):
        estimates = [node["estimate"] for node in members if node.get("estimate")]
        summaries.append(
            {
                "wave": wave,
                "node_ids": [node["id"] for node in members],
                "estimated_tasks": len(estimates),
                "estimate": {
                    field: max(estimate[field] for estimate in estimates)
                    for field in ("min_minutes", "max_minutes")
                }
                if len(estimates) == len(members)
                else None,
            }
        )
    for group in cycles.values():
        group["iterations"].sort()
    return {"waves": summaries, "cycles": list(cycles.values())}
