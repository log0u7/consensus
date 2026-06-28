from src.llm import parse_json


def test_plain_object():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_array_top_level():
    assert parse_json("[1, 2, 3]") == [1, 2, 3]


def test_fenced_json():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_fenced_with_inner_backticks():
    # The old split-on-``` logic broke here; raw_decode handles it.
    assert parse_json('```json\n{"code": "use `make` here"}\n```') == {"code": "use `make` here"}


def test_prose_before_and_after():
    assert parse_json('Here you go:\n{"verdict": "APPROVE"}\nThanks!') == {"verdict": "APPROVE"}


def test_braces_inside_string():
    assert parse_json('{"code": "if (x) { y(); }"}') == {"code": "if (x) { y(); }"}


def test_garbage_returns_none():
    assert parse_json("no json at all") is None


def test_empty_returns_none():
    assert parse_json("") is None
