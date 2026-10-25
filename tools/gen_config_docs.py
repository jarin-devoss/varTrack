"""
tools/gen_config_docs.py
────────────────────────
Writes the rule options reference from proto/vartrack/v1/models/rule.proto, so
the docs say what the code does: each option's description is its comment in
rule.proto, and where it may be set and how it combines across layers come from
its (vartrack.v1.config) annotation — the same annotation the orchestrator, the
gateway and the watcher read.

It writes docs/configuration/rule-options.md, and the generated lists between
`<!-- rule-options:NAME -->` and `<!-- /rule-options:NAME -->` in the other
configuration pages.

Run from orchestrator-service (it uses the generated Python stubs and the
orchestrator's own reading of the annotations), with buf on the PATH:

    cd orchestrator-service
    PYTHONPATH=app/generated/proto/python python ../tools/gen_config_docs.py
    PYTHONPATH=app/generated/proto/python python ../tools/gen_config_docs.py --check

--check writes nothing and exits 1 when a page is out of date.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from google.protobuf import descriptor_pb2

ROOT = Path(__file__).resolve().parent.parent
RULE_PROTO = "vartrack/v1/models/rule.proto"
REFERENCE = ROOT / "docs" / "configuration" / "rule-options.md"
PAGES_WITH_LISTS = [
    ROOT / "docs" / "configuration" / "per-repo-overrides.md",
    ROOT / "docs" / "configuration" / "schema-registry.md",
    ROOT / "docs" / "configuration" / "index.md",
]

# Fields that are not options a person writes.
SKIP = {"last_applied_at"}

# Field descriptor numbers in a FileDescriptorProto's source code info.
_MESSAGE_TYPE, _FIELD = 4, 2


def _image() -> descriptor_pb2.FileDescriptorSet:
    """The protos with their comments, as buf builds them."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "image.binpb"
        subprocess.run(["buf", "build", "-o", str(out)], cwd=ROOT, check=True)
        image = descriptor_pb2.FileDescriptorSet()
        image.ParseFromString(out.read_bytes())
        return image


def _clean(comment: str) -> str:
    """A proto comment as Markdown: section rulers dropped, paragraphs kept."""
    lines = []
    for line in comment.splitlines():
        if re.fullmatch(r"\s*─+.*─+\s*", line) or re.fullmatch(r"\s*[─]+\s*", line):
            continue
        lines.append(line[1:] if line.startswith(" ") else line)
    text = "\n".join(lines).strip("\n")
    return text


def _rule_fields(image: descriptor_pb2.FileDescriptorSet) -> list[tuple[descriptor_pb2.FieldDescriptorProto, str]]:
    proto = next(f for f in image.file if f.name == RULE_PROTO)
    index = next(i for i, m in enumerate(proto.message_type) if m.name == "Rule")
    comments: dict[int, str] = {}
    for loc in proto.source_code_info.location:
        path = list(loc.path)
        if len(path) == 4 and path[:3] == [_MESSAGE_TYPE, index, _FIELD]:
            comments[path[3]] = loc.leading_comments
    message = proto.message_type[index]
    return [(field, _clean(comments.get(i, ""))) for i, field in enumerate(message.field)
            if field.name not in SKIP]


def _type(name: str) -> str:
    from app.pipeline import rule_options

    words = {bool: "boolean", str: "string", int: "number", float: "number",
             dict: "object", list: "list"}
    kinds = rule_options.TYPES.get(name, ())
    shown = []
    for kind in kinds:
        word = words.get(kind, kind.__name__)
        if word not in shown:
            shown.append(word)
    if name in rule_options.STRING_MAPS:
        return "object (string → string)"
    if name == "prune":
        return "boolean, or object (`enabled`, `dry_run`, `protection`)"
    if name == "sync_mode":
        return "string: `AUTO`, `GIT_UPSERT_ALL`, `GIT_SMART_REPAIR`, `LIVE_STATE`"
    return " or ".join(shown) or "—"


# platform and datasource name the rule: a bundle.json rule says which
# config.cue rule it layers by them; they are not settings that change.
IDENTITY = {"platform", "datasource"}


def _where(name: str) -> tuple[str, str, str]:
    from app.pipeline import rule_options

    if name in IDENTITY:
        return "yes", "names the rule", "—"
    bundle = "—" if name in rule_options.ADMIN_ONLY else "yes"
    repo = "yes" if name in rule_options.REPO_OVERRIDABLE else "—"
    return "yes", bundle, repo


def _combine(name: str) -> str:
    from app.pipeline import rule_options

    if name in IDENTITY:
        return "names the rule (with `platform`/`datasource`): not changed by a layer"
    if name in rule_options.ADMIN_ONLY:
        return "only `config.cue` sets it"
    if name in rule_options.MERGEABLE:
        return "merged key by key" + ("; `protection` patterns add up" if name == "prune" else "")
    return "the upper layer replaces it"


def _old_names(name: str) -> list[str]:
    from app.pipeline import rule_options

    return sorted(old for old, new in rule_options.RENAMED.items() if new == name)


def _anchor(name: str) -> str:
    """The id MkDocs gives the `## `name`` heading of an option."""
    return name


def reference(fields) -> str:
    out = [
        "# Rule options",
        "",
        "<!-- Generated by tools/gen_config_docs.py from proto/vartrack/v1/models/rule.proto."
        " Edit the comments there, then run the tool. -->",
        "",
        "Every option a rule can have, and where it may be set. A rule is built from up to three"
        " files, each laid over the one before ([how](index.md#how-a-rule-is-built)):"
        " `config.cue`, the tenant's `bundle.json`, and the pushed repository's `vartrack.json`;"
        " then `config.cue`'s `force` block.",
        "",
        "| Column | Meaning |",
        "|---|---|",
        "| **config.cue** | The rule in the bundle every service reads. Every option can be set here. |",
        "| **bundle.json** | The tenant's layer, in its schema repository. An option only `config.cue` sets is dropped from it with a warning. |",
        "| **vartrack.json** | The repository's layer. Any other option there is ignored and logged. |",
        "| **Across layers** | What happens when two layers set the option. |",
        "",
        "| Option | Type | config.cue | bundle.json | vartrack.json | Across layers |",
        "|---|---|---|---|---|---|",
    ]
    for field, _ in fields:
        cue, bundle, repo = _where(field.name)
        out.append(f"| [`{field.name}`](#{_anchor(field.name)}) | {_type(field.name)} | {cue} | {bundle} | {repo} "
                   f"| {_combine(field.name)} |")
    out.append("")
    for field, comment in fields:
        cue, bundle, repo = _where(field.name)
        out += [f"## `{field.name}`", ""]
        if comment:
            out += [_indent_code(comment), ""]
        set_in = ", ".join(n for n, ok in (("`config.cue`", cue), ("`bundle.json`", bundle),
                                           ("`vartrack.json`", repo)) if ok != "—")
        out += [
            "| | |",
            "|---|---|",
            f"| Type | {_type(field.name)} |",
            f"| Set in | {set_in} |",
            f"| Across layers | {_combine(field.name)} |",
        ]
        old = _old_names(field.name)
        if old:
            out.append(f"| Former names | {', '.join(f'`{o}`' for o in old)} — still read, with a warning |")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _indent_code(comment: str) -> str:
    """A comment whose lines are laid out in columns stays as written: in a code block."""
    paragraphs = comment.split("\n\n")
    shown = []
    for para in paragraphs:
        lines = para.splitlines()
        if any(line.startswith("  ") or "→" in line or "  " in line.strip() for line in lines):
            shown.append("```text\n" + para + "\n```")
        else:
            shown.append(" ".join(line.strip() for line in lines))
    return "\n\n".join(shown)


def lists() -> dict[str, str]:
    from app.pipeline import rule_options

    def bullets(names) -> str:
        return "\n".join(f"- [`{n}`](rule-options.md#{_anchor(n)})" for n in sorted(names))

    return {
        "repo-overridable": bullets(rule_options.REPO_OVERRIDABLE),
        "admin-only": bullets(rule_options.ADMIN_ONLY),
        "mergeable": bullets(rule_options.MERGEABLE),
    }


def _fill(text: str, blocks: dict[str, str]) -> str:
    for name, body in blocks.items():
        text = re.sub(
            rf"(<!-- rule-options:{name} -->\n)(?:.*?\n)?(<!-- /rule-options:{name} -->)",
            lambda m: m.group(1) + body + "\n" + m.group(2), text, flags=re.S,
        )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when a page is out of date; write nothing")
    args = parser.parse_args()

    fields = _rule_fields(_image())
    wanted = {REFERENCE: reference(fields)}
    blocks = lists()
    for page in PAGES_WITH_LISTS:
        if not page.exists():
            continue
        current = page.read_text(encoding="utf-8").replace("\r\n", "\n")
        wanted[page] = _fill(current, blocks)

    stale = []
    for path, text in wanted.items():
        current = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else ""
        if current != text:
            stale.append(path.relative_to(ROOT).as_posix())
            if not args.check:
                path.write_text(text, encoding="utf-8", newline="\n")
    if args.check and stale:
        print("out of date, run tools/gen_config_docs.py:", ", ".join(stale), file=sys.stderr)
        return 1
    for name in stale:
        print("wrote", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
