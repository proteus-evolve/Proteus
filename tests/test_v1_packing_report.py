"""Regrade the exact private-verifier transport, including invalid output files."""
import json
from types import SimpleNamespace

import pytest

from proteus.benchmarks.coral_circle_packing import verify
from proteus.benchmarks.coral_circle_packing.report import geometry_at


@pytest.mark.parametrize('case',['valid','overlap','claimed_total','nonfinite','malformed','missing','oversize','bom'])
def test_saved_geometry_regrade_matches_private_verifier_main(tmp_path,monkeypatch,case):
    geometry={'centers':[[(i%6+.5)/6,(i//6+.5)/6] for i in range(26)],'radii':[1/12]*26}
    if case=='overlap':
        geometry['centers'][1]=geometry['centers'][0]
    if case=='claimed_total':
        geometry.update(sum_radii=999999,score=999999)
    if case=='nonfinite':
        geometry['centers'][0][0]=float('nan')
    data=json.dumps(geometry).encode()
    if case=='malformed':
        data=b'{not json'
    if case=='oversize':
        data+=b' '*256001
    if case=='bom':
        data=b'\xef\xbb\xbf'+data
    trial=tmp_path/'trial'
    artifact=trial/'artifacts/logs/artifacts/packing.json'
    artifact.parent.mkdir(parents=True)
    if case!='missing':
        artifact.write_bytes(data)
    def path(value):
        if value=='/logs/artifacts/packing.json':
            return artifact
        assert value=='/logs/verifier'
        return trial/'verifier'
    monkeypatch.setattr(verify,'Path',path)
    verify.main()
    recorded=json.loads((trial/'verifier/packing-verification.json').read_text())
    run=SimpleNamespace(root=tmp_path,evidence={})
    assert geometry_at(run,'trial')==recorded
