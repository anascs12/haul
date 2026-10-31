"""Run: python3 tests/test_segments.py"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from haul.segments import cells  # noqa: E402

assert cells("", 8, 4) == [] and cells("ff", 0, 4) == []
assert cells("ff", 8, 8) == [1.0] * 8                    # all pieces done
assert cells("80", 8, 8) == [1.0] + [0.0] * 7            # the first piece is the high bit
assert cells("01", 8, 8) == [0.0] * 7 + [1.0]            # the last piece is the low bit
assert cells("f0", 8, 2) == [1.0, 0.0]                   # pieces group into wider cells
assert cells("c0", 8, 2) == [0.5, 0.0]                   # a part-filled cell
assert cells("c0", 2, 40) == [1.0, 1.0]                  # never more cells than pieces
assert cells("ff", 16, 4) == [1.0, 1.0, 0.0, 0.0]        # a short bitfield reads as not done
print("ALL PASSED")
