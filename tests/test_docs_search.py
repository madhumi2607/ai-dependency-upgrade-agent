"""Tests for docs_search: chunking, keyword ranking, fusion, and the MCP server.

Hermetic: a tiny hand-written corpus and a deterministic fake embedding function, so no
download and no model. Retrieval quality on the real guide was measured separately (see the
RRF_K comment in mcp_servers/docs_search/index.py).
"""

import asyncio
import hashlib
from pathlib import Path

import pytest
from mcp import Client

from mcp_servers.docs_search.corpus import Source, chunk_document, chunk_markdown, rst_slug, slugify
from mcp_servers.docs_search.index import BM25, DocsIndex, reciprocal_rank_fusion, tokenize
from mcp_servers.docs_search.server import build_server

SOURCE = Source(id="guide", library="pydantic", url="https://example.invalid/guide.md", docs_url="https://docs.example/migration/")

GUIDE = """\
# Migration guide

Intro text.

## Changes to config

`orm_mode` was renamed to `from_attributes`.

```py
# V1
class Config:
    orm_mode = True
# V2
model_config = ConfigDict(from_attributes=True)
```

## `BaseSettings` has moved to `pydantic-settings`

Install pydantic-settings and import BaseSettings from it.

## Removed in Pydantic V2

- `pydantic.fields.ModelField`
- `pydantic.typing.is_classvar`

### Making a field optional

Give the field a default value to make it optional.
"""


class FakeEmbedding:
    """Deterministic bag-of-words hashing embedding: shared words -> similar vectors."""

    def __call__(self, input: list[str]) -> list[list[float]]:
        vectors = []
        for text in input:
            v = [0.0] * 64
            for token in tokenize(text):
                v[int(hashlib.md5(token.encode()).hexdigest(), 16) % 64] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            vectors.append([x / norm for x in v])
        return vectors

    @staticmethod
    def name() -> str:
        return "fake-hash"

    def is_legacy(self) -> bool:
        return False

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine"]

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict) -> "FakeEmbedding":
        return FakeEmbedding()

    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self(input)


def test_chunking_follows_headings_and_ignores_code_comments() -> None:
    chunks = chunk_markdown(GUIDE, SOURCE)
    titles = [c.title for c in chunks]
    assert titles == [
        "Migration guide",
        "Migration guide › Changes to config",
        "Migration guide › `BaseSettings` has moved to `pydantic-settings`",
        "Migration guide › Removed in Pydantic V2",
        "Migration guide › Removed in Pydantic V2 › Making a field optional",
    ]
    config = chunks[1]
    assert "# V1" in config.text and "# V2" in config.text  # code comments stayed in the section
    assert config.url == "https://docs.example/migration/#changes-to-config"


def test_slugify_matches_mkdocs_style() -> None:
    assert slugify("`BaseSettings` has moved to `pydantic-settings`") == "basesettings-has-moved-to-pydantic-settings"


def test_long_sections_split_but_never_inside_code() -> None:
    body = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(12))
    code = "```py\n" + "\n\nx = 1\n" * 30 + "```"
    chunks = chunk_markdown(f"## Big\n\n{body}\n\n{code}\n", SOURCE, max_chars=800)
    assert len(chunks) > 2 and all(c.text.count("```") % 2 == 0 for c in chunks)


def test_tokenize_keeps_identifiers_whole() -> None:
    assert tokenize("pydantic.fields.ModelField orm_mode __fields__") == ["pydantic", "fields", "modelfield", "orm_mode", "__fields__"]


def test_bm25_finds_exact_identifiers() -> None:
    docs = ["config changes from_attributes orm_mode", "settings moved", "removed is_classvar ModelField"]
    bm25 = BM25(docs)
    assert bm25.rank("is_classvar")[0] == 2
    assert bm25.rank("orm_mode")[0] == 0
    assert bm25.rank("nothing matches this") == []


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    # Item 2 is near the top of both lists, so it beats items that top only one list.
    assert reciprocal_rank_fusion([[1, 2, 3], [4, 2, 5]])[0] == 2


def test_weighted_fusion_lets_the_keyword_top_hit_win() -> None:
    # Keyword's #1 (an exact identifier match, absent from the semantic list) vs item 2, which is
    # only 5th in both lists. Equal weights: 2 scores 2/10 > 7's 1/6. Keyword x2: 7 scores 2/6 > 3/10.
    keyword, semantic = [7, 11, 12, 13, 2], [4, 5, 6, 8, 2]
    assert reciprocal_rank_fusion([keyword, semantic])[0] == 2
    assert reciprocal_rank_fusion([keyword, semantic], weights=[2.0, 1.0])[0] == 7


@pytest.fixture
def index(tmp_path: Path) -> DocsIndex:
    idx = DocsIndex(tmp_path / "index", embedding_function=FakeEmbedding())
    idx.build(chunk_markdown(GUIDE, SOURCE))
    return idx


def test_search_returns_the_right_section(index: DocsIndex) -> None:
    assert "Removed in Pydantic V2" in index.search("pydantic.typing.is_classvar", k=2)[0].title
    assert "Changes to config" in index.search("orm_mode", k=2)[0].title
    assert "BaseSettings" in index.search("BaseSettings", k=2)[0].title
    assert index.search("orm_mode", library="other-lib") == []


def test_index_persists_between_instances(index: DocsIndex) -> None:
    reopened = DocsIndex(index.path, embedding_function=FakeEmbedding())
    assert len(reopened.chunks) == 5 and "Changes to config" in reopened.search("orm_mode")[0].title


def test_server_over_mcp(index: DocsIndex) -> None:
    async def scenario() -> None:
        async with Client(build_server(index)) as client:
            assert [t.name for t in (await client.list_tools()).tools] == ["search_docs"]
            result = await client.call_tool("search_docs", {"query": "ModelField", "k": 2})
            top = result.structured_content["results"][0]
            assert "Removed in Pydantic V2" in top["title"] and top["url"].endswith("#removed-in-pydantic-v2")
            empty = await client.call_tool("search_docs", {"query": "  "})
            assert empty.is_error

    asyncio.run(scenario())


def test_system_prompt_mentions_docs_only_when_available() -> None:
    from agent.prompts import system_prompt

    assert "search_docs" in system_prompt("pydantic", "1", "2", has_docs=True)
    assert "search_docs" not in system_prompt("pydantic", "1", "2", has_docs=False)


def test_heading_paths_without_a_top_level_title() -> None:
    """The real Pydantic guide starts at '##': sibling sections must not nest under the first one."""
    doc = "## Install\n\nx\n\n## Migration guide\n\ny\n\n### Changes to config\n\nz\n\n## Removed\n\nw\n"
    assert [c.title for c in chunk_markdown(doc, SOURCE)] == [
        "Install", "Migration guide", "Migration guide › Changes to config", "Removed",
    ]


RST_SOURCE = Source(id="sqla", library="sqlalchemy", url="https://example.invalid/m.rst",
                    docs_url="https://docs.example/migration_20.html", format="rst")

RST_GUIDE = """\
.. _migration_20_toplevel:

======================================
SQLAlchemy 2.0 - Major Migration Guide
======================================

Intro text.

Overview
========

Overview text.

Library-level "Autocommit" removed
----------------------------------

Use ``engine.begin()``::

    with engine.begin() as conn:
        conn.execute(text("x"))
    Not a heading
    =============

ORM Query - get() method moves to Session
-----------------------------------------

Use ``session.get()``.

Migrating an Existing Mapping
=============================

Mapping text.
"""


def test_rst_chunking_nests_by_adornment_style() -> None:
    chunks = chunk_document(RST_GUIDE, RST_SOURCE)
    titles = [c.title for c in chunks]
    top = "SQLAlchemy 2.0 - Major Migration Guide"
    assert titles == [
        "sqla",  # the label before the first title
        top,
        f"{top} › Overview",
        f'{top} › Overview › Library-level "Autocommit" removed',
        f"{top} › Overview › ORM Query - get() method moves to Session",
        f"{top} › Migrating an Existing Mapping",
    ]
    # An underlined line inside an indented code block is code, not a section title.
    assert "Not a heading" in chunks[3].text
    assert chunks[4].url == "https://docs.example/migration_20.html#orm-query-get-method-moves-to-session"


def test_rst_slug_matches_sphinx_style() -> None:
    assert rst_slug("Migration to 2.0 Step Six - Add ``__allow_unmapped__``") == "migration-to-2-0-step-six-add-allow-unmapped"


def test_server_only_searches_the_task_library(tmp_path: Path) -> None:
    idx = DocsIndex(tmp_path / "mixed", embedding_function=FakeEmbedding())
    idx.build(chunk_markdown(GUIDE, SOURCE) + chunk_document(RST_GUIDE, RST_SOURCE))

    async def scenario() -> None:
        async with Client(build_server(idx, library="sqlalchemy")) as client:
            result = await client.call_tool("search_docs", {"query": "orm_mode get() Autocommit", "k": 8})
            titles = [r["title"] for r in result.structured_content["results"]]
            assert titles and all("SQLAlchemy" in t or t == "sqla" for t in titles)

    asyncio.run(scenario())
