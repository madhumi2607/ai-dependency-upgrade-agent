"""Tests for the migration-completeness log parser (benchmark/completeness.py)."""

from benchmark.completeness import parse_deprecations

LOG = """\
============================= test session starts ==============================
collected 3 items

tests/test_models.py ...                                                 [100%]

=============================== warnings summary ===============================
tests/test_models.py::test_a
  /work/pkg/models.py:12: PydanticDeprecatedSince20: Pydantic V1 style `@validator` validators are deprecated.
    @validator("x")

tests/test_models.py::test_a
tests/test_models.py::test_b
  /work/pkg/api.py:40: PydanticDeprecatedSince20: The `dict` method is deprecated; use `model_dump` instead.
    return model.dict()

tests/test_models.py::test_b
  /work/tests/test_models.py:9: PydanticDeprecatedSince20: The `json` method is deprecated
    m.json()

tests/test_models.py::test_c
  /usr/local/lib/python3.11/site-packages/somelib/x.py:3: DeprecationWarning: pkg_resources is deprecated

tests/test_models.py::test_c
  /work/pkg/util.py:5: DeprecationWarning: datetime.utcnow() is deprecated
    now = datetime.utcnow()

tests/test_models.py::test_c
  /work/pkg/models.py:30: UserWarning: not a deprecation

tests/test_models.py::test_c
  /venv/lib/python3.11/site-packages/pydantic/_internal/_config.py:323: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead.
    warnings.warn(DEPRECATION_MESSAGE, DeprecationWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================== 3 passed, 4 warnings in 0.12s =========================
"""


def test_counts_deprecated_call_sites_in_repo_source_only() -> None:
    sites = parse_deprecations(LOG, "pydantic")
    assert [s["location"] for s in sites["library"]] == ["pkg/models.py:12", "pkg/api.py:40"]
    assert [s["location"] for s in sites["other"]] == ["pkg/util.py:5"]  # not about pydantic
    # Reported inside pydantic itself: counted separately, not dropped with other site-packages warnings.
    assert [s["category"] for s in sites["library_internal"]] == ["PydanticDeprecatedSince20"]


def test_no_warnings_summary_means_nothing_found() -> None:
    assert parse_deprecations("==== 3 passed in 0.1s ====", "pydantic") == {"library": [], "other": [], "library_internal": []}


def test_sqlalchemy_warnings_are_classified_by_category() -> None:
    log = "=== warnings summary ===\nt.py::x\n  /work/lib/db.py:7: SADeprecationWarning: The Query.get() method is considered legacy\n"
    assert [s["location"] for s in parse_deprecations(log, "sqlalchemy")["library"]] == ["lib/db.py:7"]


def test_trace_export_scrubs_local_paths_and_emails() -> None:
    from benchmark.export_traces import scrub
    from benchmark.runner import PROJECT_ROOT

    root = str(PROJECT_ROOT)
    text = json_text = f'{{"repo": "{root.replace(chr(92), chr(92) * 2)}\\workspace\\x", "a": "{root}/y", "b": "dev@example.org"}}'
    out = scrub(json_text)
    assert root not in out and root.replace("\\", "\\\\") not in out and "example.org" not in out
    assert out.count("<project>") == 2 and "<email>" in out
