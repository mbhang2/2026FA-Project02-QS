"""A null that knows which candidate it is.

Task 30 measured the cost of not having one: BH fired in 23 of 40 known-null
campaigns against a promised 0.05, because the table was tabulated from one
reference strategy and a campaign points hundreds of different ones at it. The
reference gets 0/40; its variants get 10x.

See `rotation` for the design and `candidate` for the null itself.
"""

from .candidate import CandidateNull, candidate_null, profile_of
from .rotation import RotatedDataset, rotate_session

__all__ = ["RotatedDataset", "rotate_session", "candidate_null", "CandidateNull",
           "profile_of"]
