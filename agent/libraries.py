"""What the agent needs to know about each library it can migrate, in one place.

Adding a library = adding a profile here and its docs in mcp_servers/docs_search/corpus.py.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LibraryProfile:
    # Prompt text: what "routing around the migration" looks like for this library.
    no_shortcuts: str
    # Regexes over added diff lines that mean the old API was kept alive (review check: compat_shim).
    shim_patterns: list[str] = field(default_factory=list)


PROFILES: dict[str, LibraryProfile] = {
    "pydantic": LibraryProfile(
        no_shortcuts="no `pydantic.v1` imports",
        shim_patterns=[r"\bpydantic\.v1\b", r"from\s+pydantic\s+import\s+v1\b"],
    ),
    "sqlalchemy": LibraryProfile(
        no_shortcuts=(
            "use the 2.0-style APIs (`text()` for string SQL, explicit `connection.execute()` inside a "
            "transaction, `select(...)` positional columns, `Row._mapping` for dict access); do not keep "
            "SQLAlchemy pinned below 2.0"
        ),
        # 2.0 has no compatibility namespace to import, so pins (checks.check_pins) are the main guard.
        shim_patterns=[],
    ),
}

GENERIC = LibraryProfile(no_shortcuts="no compatibility layers that keep the old API alive")


def profile(library: str) -> LibraryProfile:
    return PROFILES.get(library.lower(), GENERIC)
