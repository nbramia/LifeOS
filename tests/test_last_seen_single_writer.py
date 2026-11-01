"""Ratchet: api.services.person_stats.sync_last_seen is the only writer of last_seen.

The column holds a value derived from the interactions table, so any other source
would make it disagree with that table. Production code may read last_seen anywhere
but writes it in exactly one place.

Scans every .py file under api/, scripts/, and config/, plus mcp_server.py and the
shell scripts under scripts/, for four kinds of write:

- assigning to an attribute named last_seen, or setattr() with that name
- passing last_seen= when constructing a PersonEntity
- SQL that sets or inserts the column (last_seen_together is a different column)
- the store's own column list, checked directly

To add a genuinely unrelated attribute that happens to be called last_seen, add it
to UNRELATED_ATTRIBUTE_WRITES with the reason. Never add a person-entity write.
"""
import ast
import re
from pathlib import Path

import pytest

from api.services.person_entity import PersonEntityStore

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent

THE_WRITER = ("api/services/person_stats.py", "sync_last_seen")

# (file, class or function holding the write) -> why it isn't a person entity.
UNRELATED_ATTRIBUTE_WRITES = {
    # A chat participant being parsed out of an export, stored as source metadata.
    ("api/services/whatsapp_import.py", "parse_whatsapp_export"): "WhatsApp export participant",
}

_SQL_WRITE = re.compile(
    r"\bUPDATE\s+\S*person_entities\b[^;]*\blast_seen\b(?!_)"
    r"|\bINSERT\b[^;]*\bperson_entities\b[^;]*\blast_seen\b(?!_)"
    r"|\bSET\b[^;]*\blast_seen\b(?!_)\s*=",
    re.IGNORECASE | re.DOTALL,
)


def _python_files() -> list[Path]:
    files = [REPO / "mcp_server.py"]
    for root in ("api", "scripts", "config"):
        files.extend((REPO / root).rglob("*.py"))
    return sorted(files)


def _enclosing_names(tree: ast.AST) -> dict[ast.AST, str]:
    """Map every node to the name of the innermost function or class holding it."""
    owners: dict[ast.AST, str] = {}

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name
            owners[child] = name
            visit(child, name)

    visit(tree, "<module>")
    return owners


def find_writes(source: str, relative_path: str) -> list[tuple[str, str, int, str]]:
    """Every last_seen write in a Python source, as (file, owner, line, kind)."""
    tree = ast.parse(source)
    owners = _enclosing_names(tree)
    # Prose in a docstring or bare string can name a statement without running it.
    bare_strings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
    writes = []

    def record(node: ast.AST, kind: str) -> None:
        writes.append((relative_path, owners.get(node, "<module>"), node.lineno, kind))

    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr == "last_seen":
                record(node, "attribute")

        if isinstance(node, ast.Call):
            func = node.func
            callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if (
                callee == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "last_seen"
            ):
                record(node, "setattr")
            if callee in {"PersonEntity", "cls"} and any(
                keyword.arg == "last_seen" for keyword in node.keywords
            ):
                record(node, "constructor")

        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in bare_strings
            and _SQL_WRITE.search(node.value)
        ):
            record(node, "sql")

    return writes


def _production_writes() -> list[tuple[str, str, int, str]]:
    writes = []
    for path in _python_files():
        source = path.read_text()
        if "last_seen" in source:
            writes.extend(find_writes(source, path.relative_to(REPO).as_posix()))
    return writes


def test_sync_last_seen_is_the_only_writer():
    allowed = set(UNRELATED_ATTRIBUTE_WRITES) | {THE_WRITER}
    offenders = [w for w in _production_writes() if (w[0], w[1]) not in allowed]
    assert not offenders, (
        "last_seen has one writer, person_stats.sync_last_seen(). "
        f"Remove these writes: {offenders}"
    )


def test_the_writer_is_where_this_test_says_it_is():
    writes = [w for w in _production_writes() if w[3] == "sql"]
    assert [(w[0], w[1]) for w in writes] == [THE_WRITER]


def test_every_unrelated_entry_is_still_needed():
    found = {(w[0], w[1]) for w in _production_writes()}
    stale = set(UNRELATED_ATTRIBUTE_WRITES) - found
    assert not stale, f"Remove these unused entries: {stale}"


def test_shell_scripts_do_not_write_it():
    offenders = [
        path.relative_to(REPO).as_posix()
        for path in (REPO / "scripts").rglob("*.sh")
        if _SQL_WRITE.search(path.read_text(errors="ignore"))
    ]
    assert not offenders


def test_the_store_does_not_persist_it():
    assert "last_seen" not in PersonEntityStore._COLUMNS
    assert "last_seen" not in PersonEntityStore._UPSERT_SQL


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("person.last_seen = now\n", "attribute"),
        ("person.last_seen += delta\n", "attribute"),
        ("setattr(person, 'last_seen', now)\n", "setattr"),
        ("PersonEntity(id='x', last_seen=now)\n", "constructor"),
        ("api.PersonEntity(last_seen=now)\n", "constructor"),
        ("conn.execute('UPDATE person_entities SET last_seen = ? WHERE id = ?')\n", "sql"),
        ("conn.execute('UPDATE person_entities SET company = ?, last_seen = ?')\n", "sql"),
        ("conn.execute('update person_entities set last_seen = NULL')\n", "sql"),
        ("conn.execute(f'UPDATE {table} SET last_seen = ?')\n", "sql"),
        ("conn.execute('INSERT INTO person_entities (id, last_seen) VALUES (?, ?)')\n", "sql"),
        ("conn.execute('INSERT OR REPLACE INTO person_entities (id, last_seen) VALUES (?, ?)')\n", "sql"),
    ],
)
def test_detects(source, kind):
    assert [write[3] for write in find_writes(source, "x.py")] == [kind]


@pytest.mark.parametrize(
    "source",
    [
        "value = person.last_seen\n",
        "response = PersonResponse(last_seen=person.last_seen)\n",
        "data['last_seen'] = person.last_seen\n",
        "conn.execute('SELECT id, last_seen FROM person_entities ORDER BY last_seen DESC')\n",
        "conn.execute('CREATE INDEX idx ON person_entities(last_seen)')\n",
        "conn.execute('UPDATE relationships SET last_seen_together = ?')\n",
        "rel.last_seen_together = now\n",
        "PersonEntity(id='x', first_seen=now)\n",
    ],
)
def test_ignores(source):
    assert find_writes(source, "x.py") == []
