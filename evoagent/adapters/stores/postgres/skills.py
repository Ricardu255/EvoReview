"""PostgreSQL skill adapter; SQL is shared through the dialect-aware mixin."""
from ..shared.skills import SkillStoreMixin


class PostgresSkillStoreMixin(SkillStoreMixin):
    pass
