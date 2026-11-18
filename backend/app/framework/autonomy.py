from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.framework.base import TaskDefinition
from app.models import TaskConfig
from app.store import get_setting


@dataclass
class EffectiveAutonomy:
    level: int
    selected: int
    task_cap: int
    global_cap: int
    code_max: int
    limited_by: str | None  # which limit reduced the selected level
    kill_switch: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def effective_autonomy(db: Session, defn: TaskDefinition, cfg: TaskConfig) -> EffectiveAutonomy:
    global_cap = int(get_setting(db, "global_autonomy_cap"))
    limits = {
        "task cap": cfg.autonomy_cap,
        "global cap": global_cap,
        "task definition maximum": defn.max_autonomy,
    }
    level = cfg.autonomy_level
    limited_by = None
    for name, cap in limits.items():
        if cap < level:
            level, limited_by = cap, name
    return EffectiveAutonomy(level=level, selected=cfg.autonomy_level, task_cap=cfg.autonomy_cap,
                             global_cap=global_cap, code_max=defn.max_autonomy, limited_by=limited_by,
                             kill_switch=bool(get_setting(db, "kill_switch")))
