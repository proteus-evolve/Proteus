"""Standalone private verifier; never imports or executes candidate code.

Geometry and the normalization constant follow the pinned CORAL grader.
The transport and error handling are Proteus adaptations documented in NOTICE.
"""

import json
import math
from pathlib import Path

N = 26
BENCHMARK = 2.635977
TOLERANCE = 1e-6


def grade(value):
    try:
        raw_centers, raw_radii = value['centers'], value['radii']
        if (not isinstance(raw_centers, (list, tuple)) or not isinstance(raw_radii, (list, tuple))
                or len(raw_centers) != N or len(raw_radii) != N
                or any(not isinstance(row, (list, tuple)) or len(row) != 2 for row in raw_centers)):
            return {'score': 0.0, 'details': 'Invalid centers or radii shape'}
        centers = [[float(v) for v in row] for row in raw_centers]
        radii = [float(v) for v in raw_radii]
        if not all(math.isfinite(v) for row in centers for v in row) or not all(math.isfinite(v) for v in radii):
            return {'score': 0.0, 'details': 'Non-finite coordinates or radii'}
        if any(radius < 0 for radius in radii):
            return {'score': 0.0, 'details': 'Negative radii'}
        for i, ((x, y), radius) in enumerate(zip(centers, radii)):
            if min(x-radius, y-radius) < -TOLERANCE or max(x+radius, y+radius) > 1+TOLERANCE:
                return {'score': 0.0, 'details': f'Circle {i} outside unit square'}
        for i in range(N):
            for j in range(i+1, N):
                distance = math.sqrt(sum((a-b)**2 for a, b in zip(centers[i], centers[j])))
                if distance < radii[i]+radii[j]-TOLERANCE:
                    return {'score': 0.0, 'details': f'Overlap between circles {i} and {j}'}
        # The claimed sum is deliberately ignored, as in the reference grader.
        total = sum(radii)
        return {'score': total/BENCHMARK, 'sum_radii': total, 'details': 'Valid packing'}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {'score': 0.0, 'details': 'Malformed numeric packing'}


def main():
    path = Path('/logs/artifacts/packing.json')
    try:
        if path.stat().st_size > 256000:
            raise ValueError('packing output exceeds the domain transport limit')
        result = grade(json.loads(path.read_text()))
    except (OSError, ValueError):
        result = {'score': 0.0, 'details': 'Missing or malformed packing output'}
    target = Path('/logs/verifier')
    target.mkdir(parents=True, exist_ok=True)
    (target/'reward.txt').write_text(str(result['score']))
    (target/'packing-verification.json').write_text(json.dumps(result, allow_nan=False)+'\n')
    print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    main()
