"""Retrieval evaluation: how often the right section is in the top k, per ranking strategy.

Queries are phrased the way the agent actually searches (identifiers from errors and old
code), plus a few questions in words. A hit = a top-k chunk whose heading path (target
"title:...") or text (target "text:...") contains the target.

    uv run python -m mcp_servers.docs_search.eval
"""

from mcp_servers.docs_search.build import index_path
from mcp_servers.docs_search.index import DocsIndex, reciprocal_rank_fusion

PYDANTIC: dict[str, str] = {
    # identifiers, as they appear in errors / old code
    "pydantic.typing is_classvar": "text:is_classvar",
    "pydantic.fields.ModelField": "text:ModelField",
    "pydantic.error_wrappers.ErrorWrapper": "text:ErrorWrapper",
    "orm_mode": "title:Changes to config",
    "allow_population_by_field_name": "text:populate_by_name",
    "__fields__": "text:model_fields",
    "parse_obj": "text:model_validate",
    "update_forward_refs": "text:model_rebuild",
    "json_encoders": "text:json_encoders",
    "GenericModel": "title:GenericModel",
    "BaseSettings": "title:BaseSettings",
    "conlist min_items": "title:Constrained types",
    "root_validator": "title:Changes to validators",
    "validator pre=True always=True": "title:Changes to validators",
    # questions in words
    "how do I make a field optional with a default": "title:Required, optional, and nullable",
    "my custom type uses __get_validators__": "title:Defining custom types",
}

SQLALCHEMY: dict[str, str] = {
    # 2.0 error messages and old-style calls
    "AttributeError: 'Engine' object has no attribute 'execute'": "title:Connectionless",
    "Not an executable object: 'SELECT * FROM users'": "text:text(",
    "select([users.c.id])": "title:select() no longer accepts varied constructor arguments",
    "Query.get": "title:get() method moves to Session",
    "autocommit=True": "title:Autocommit",
    "MetaData(bind=engine)": "title:bound metadata",
    "row['name'] TypeError tuple indices must be integers": "title:named tuples",
    "sqlalchemy.ext.declarative declarative_base": "title:Declarative becomes a first class API",
    "joinedload('addresses') string relationship": "title:uses attributes, not strings",
    "session.begin(subtransactions=True)": "title:subtransaction",
    "insert(values={...}) keyword arguments": "title:no longer accept keyword constructor arguments",
    "lazy='dynamic' relationship": "title:Write Only",
    "Type annotation can't be correctly interpreted for Annotated Declarative Table form": "title:__allow_unmapped__",
    # questions in words
    "how do I commit after executing an insert on a connection": "title:Autocommit",
    "query results contain duplicate rows after joinedload": "title:uniquified",
}

QUERIES: dict[str, dict[str, str]] = {"pydantic": PYDANTIC, "sqlalchemy": SQLALCHEMY}


def hit(chunk, target: str) -> bool:
    kind, _, want = target.partition(":")
    haystack = chunk.title if kind == "title" else chunk.text
    return want in haystack.replace("`", "")


def evaluate(index: DocsIndex, library: str, k: int = 3) -> dict[str, int]:
    """Scores one library's queries against that library's chunks (the agent's filter too)."""
    position = {c.id: i for i, c in enumerate(index.chunks)}
    allowed = {i for i, c in enumerate(index.chunks) if c.library == library}
    strategies = {
        "keyword": lambda kw, sem: kw,
        "semantic": lambda kw, sem: sem,
        "hybrid, equal weights": lambda kw, sem: reciprocal_rank_fusion([kw, sem]),
        "hybrid (as shipped)": None,  # keyword x2, see index.py
    }
    scores = {name: 0 for name in strategies}
    for query, target in QUERIES[library].items():
        result = index.collection.query(query_texts=[query], n_results=20, include=[], where={"library": library})
        sem = [position[i] for i in result["ids"][0]]
        kw = [i for i in index._bm25.rank(query) if i in allowed]
        for name, rank in strategies.items():
            top = index.search(query, k=k, library=library) if rank is None else [index.chunks[i] for i in rank(kw, sem)[:k]]
            if any(hit(c, target) for c in top):
                scores[name] += 1
            elif name == "hybrid (as shipped)":
                print(f"  miss: {query!r} (wanted {target}); got {[c.title.split(' › ')[-1] for c in top]}")
    return scores


def main() -> None:
    index = DocsIndex(index_path())
    for library, queries in QUERIES.items():
        for k in (1, 3):
            print(f"{library}: top-{k} hits out of {len(queries)}:")
            for name, score in evaluate(index, library, k).items():
                print(f"  {name:22} {score}")


if __name__ == "__main__":
    main()
