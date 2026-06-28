from src.models import ConsensusReport, PipelineResult
from src.sessions import _dump, _load


def _sample_session():
    result = PipelineResult(
        spec="do x",
        code="print(1)",
        consensus=ConsensusReport(panel=["a"], summary="ok"),
        verdict="APPROVE",
        rationale="fine",
    )
    return {
        "result": result,
        "system": "sys prompt",
        "history": [{"role": "user", "content": "hi"}],
    }


def test_dump_serializes_result_to_plain_dict():
    dumped = _dump(_sample_session())
    assert isinstance(dumped["result"], dict)
    assert dumped["result"]["verdict"] == "APPROVE"
    assert dumped["system"] == "sys prompt"
    assert dumped["history"][0]["content"] == "hi"


def test_dump_load_roundtrip_rebuilds_pipeline_result():
    loaded = _load(_dump(_sample_session()))
    assert isinstance(loaded["result"], PipelineResult)
    assert loaded["result"].verdict == "APPROVE"
    assert loaded["result"].consensus.summary == "ok"
    assert loaded["history"] == [{"role": "user", "content": "hi"}]


def test_dump_load_with_files():
    """Artifacts in the result must survive a dump/load roundtrip."""
    from src.models import Artifact
    result = PipelineResult(
        spec="multi-file",
        code="",
        files=[
            Artifact(path="main.py", language="python", content="x=1"),
            Artifact(path="lib.py",  language="python", content="y=2"),
        ],
        consensus=ConsensusReport(panel=["r1"], summary="ok"),
        verdict="APPROVE",
    )
    session = {"result": result, "system": "", "history": []}
    loaded = _load(_dump(session))
    assert isinstance(loaded["result"], PipelineResult)
    assert len(loaded["result"].files) == 2
    assert loaded["result"].files[0].path == "main.py"


def test_dump_load_empty_history():
    session = {
        "result": PipelineResult(
            spec="s", code="c",
            consensus=ConsensusReport(),
        ),
        "system": "",
        "history": [],
    }
    loaded = _load(_dump(session))
    assert loaded["history"] == []


def test_dump_result_is_json_serializable():
    """The dumped result dict must be JSON-serializable (for Postgres backend)."""
    import json
    dumped = _dump(_sample_session())
    # Should not raise
    json.dumps(dumped)
