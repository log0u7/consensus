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
