"""The research loop: propose, run, record, correct.

`agents` is the stub proposal side, `campaign` is the driver. See
`docs/design/architecture.md`.
"""

from .agents import NullGenerator, ReplayList, StubAgent
from .campaign import CampaignRecord, CampaignResult, run_campaign, summarise

__all__ = ["StubAgent", "NullGenerator", "ReplayList", "CampaignRecord",
           "CampaignResult", "run_campaign", "summarise"]
