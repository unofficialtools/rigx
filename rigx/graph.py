"""Generate a Mermaid dependency graph for a target."""

from __future__ import annotations

from rigx.config import Project


def _node_id(name: str) -> str:
    """Sanitize a target/dep name to a valid Mermaid node id."""
    return name.replace(".", "_").replace("-", "_").replace("/", "_")


def mermaid(project: Project, root_spec: str) -> str:
    """Return a Mermaid `graph TD` for the dep tree rooted at `root_spec`.

    Accepts the same forms as `rigx build` — `hello`, `frontend.app`, and
    `target@variant`. The variant suffix is dropped because rigx's schema
    only varies *flags* per variant, not deps; the dep graph is identical.

    Cross-flake refs (`frontend.app`) appear as a single opaque node; to see
    *their* sub-graph, run `rigx graph` from inside the sub-project."""
    name = root_spec.split("@", 1)[0]

    # Resolve root: own/B-merged target first, then A-style cross-flake.
    if name in project.targets:
        owning = project
        root_name = name
    elif "." in name:
        resolved = project.find_target(name)
        if resolved is None:
            raise ValueError(f"no such target: {name}")
        owning, root_name = resolved
    else:
        raise ValueError(f"no such target: {name}")

    nodes: dict[str, tuple[str, str]] = {}     # id -> (label, class)
    edges: list[tuple[str, str]] = []
    visited: set[str] = set()

    def add_node(node_id: str, label: str, klass: str) -> None:
        if node_id not in nodes:
            nodes[node_id] = (label, klass)

    def visit(qual: str) -> None:
        if qual in visited:
            return
        visited.add(qual)
        target = owning.targets[qual]
        add_node(_node_id(qual), f"{qual} [{target.kind}]", "internal")

        for d in target.deps.internal:
            if d in owning.targets:
                edges.append((_node_id(qual), _node_id(d)))
                visit(d)
            elif "." in d and d.split(".", 1)[0] in owning.local_deps:
                # Cross-flake: render as an opaque leaf. Going deeper would
                # require following another flake's loaded sub_project, which
                # is doable but bloats the graph; users can `rigx graph` from
                # inside the sibling for that.
                node_id = _node_id(d)
                add_node(node_id, f"{d} [local-dep]", "cross_flake")
                edges.append((_node_id(qual), node_id))

        for d in target.deps.nixpkgs:
            node_id = "pkg_" + _node_id(d)
            add_node(node_id, f"pkgs.{d}", "nixpkgs")
            edges.append((_node_id(qual), node_id))

        for d in target.deps.git:
            node_id = "git_" + _node_id(d)
            add_node(node_id, f"git:{d}", "git")
            edges.append((_node_id(qual), node_id))

        # `run` adds an implicit dep on the named internal target; surface it.
        if (
            target.run
            and target.run in owning.targets
            and target.run not in target.deps.internal
        ):
            edges.append((_node_id(qual), _node_id(target.run)))
            visit(target.run)

    visit(root_name)
    return _render(nodes, edges)


def _render(
    nodes: dict[str, tuple[str, str]], edges: list[tuple[str, str]]
) -> str:
    lines = ["graph TD"]
    for nid, (label, klass) in nodes.items():
        # Pkgs/git/cross_flake get rounded-rectangle shape; internal targets
        # the default rectangle. Quoting handles any spaces/brackets in
        # labels.
        if klass == "internal":
            lines.append(f'    {nid}["{label}"]')
        else:
            lines.append(f'    {nid}(["{label}"])')
    for src, dst in edges:
        lines.append(f"    {src} --> {dst}")
    # Class styling — kept inline so the output is one self-contained file.
    lines.append("    classDef internal fill:#e1f5fe,stroke:#01579b")
    lines.append("    classDef nixpkgs fill:#f3e5f5,stroke:#4a148c")
    lines.append("    classDef git fill:#fff3e0,stroke:#e65100")
    lines.append(
        "    classDef cross_flake fill:#e0f7fa,stroke:#006064,stroke-dasharray:4 2"
    )
    by_class: dict[str, list[str]] = {}
    for nid, (_, klass) in nodes.items():
        by_class.setdefault(klass, []).append(nid)
    for klass, ids in by_class.items():
        lines.append(f"    class {','.join(ids)} {klass}")
    return "\n".join(lines) + "\n"
