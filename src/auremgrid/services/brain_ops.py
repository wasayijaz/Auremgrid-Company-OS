from __future__ import annotations

from typing import Any

from auremgrid.services.brain_ops_shared import BrainOpsSharedMixin
from auremgrid.services.brain_ops_resolution import BrainOpsResolutionMixin
from auremgrid.services.brain_ops_promotion import BrainOpsPromotionMixin
from auremgrid.services.brain_ops_knowledge_state import BrainOpsKnowledgeStateMixin
from auremgrid.services.brain_ops_collections import BrainOpsCollectionsMixin
from auremgrid.services.brain_ops_saved_views import BrainOpsSavedViewsMixin
from auremgrid.services.brain_ops_validation import BrainOpsValidationMixin


class BrainOperations(
    BrainOpsSharedMixin,
    BrainOpsResolutionMixin,
    BrainOpsPromotionMixin,
    BrainOpsKnowledgeStateMixin,
    BrainOpsCollectionsMixin,
    BrainOpsSavedViewsMixin,
    BrainOpsValidationMixin,
):
    pass
