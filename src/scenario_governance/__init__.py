"""二次开发情景治理：不可变输入快照、方案比较、投决与实绩偏差。"""

from .evaluation import ALGORITHM_VERSION, evaluate
from .models import CATEGORIES, ScenarioDefinition, parse_contribution
from .service import GovernanceService

__all__ = [
    "ALGORITHM_VERSION",
    "CATEGORIES",
    "GovernanceService",
    "ScenarioDefinition",
    "evaluate",
    "parse_contribution",
]

__version__ = "0.1.0"
