"""Candidate-side adapter: export geometry for a separate private verifier."""

import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).parent/'.proteus-deps'))
import numpy as np  # noqa: E402 -- pinned optional dependency inside the subject image
import initial_program  # noqa: E402 -- editable candidate module


def main():
    request = json.load(sys.stdin)
    random.seed(request['seed'])
    np.random.seed(request['seed'] % 2**32)
    centers, radii, _ = initial_program.run()
    packing = {'centers': np.asarray(centers, dtype=float).tolist(),
               'radii': np.asarray(radii, dtype=float).tolist()}
    encoded = json.dumps(packing, allow_nan=False)
    target = Path('/logs/artifacts')
    target.mkdir(parents=True, exist_ok=True)
    (target/'packing.json').write_text(encoded+'\n')
    print(json.dumps({'output': packing, 'state': request['state']}, allow_nan=False))


if __name__ == '__main__':
    main()
