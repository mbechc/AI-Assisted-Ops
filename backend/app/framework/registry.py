from app.framework.base import TaskDefinition

REGISTRY: dict[str, TaskDefinition] = {}


def register(cls: type[TaskDefinition]) -> type[TaskDefinition]:
    inst = cls()
    if inst.key in REGISTRY:
        raise RuntimeError(f"duplicate task key {inst.key}")
    REGISTRY[inst.key] = inst
    return cls


def load_all() -> dict[str, TaskDefinition]:
    import app.usecases  # noqa: F401  (registers task definitions on import)
    return REGISTRY


def get(key: str) -> TaskDefinition:
    load_all()
    if key not in REGISTRY:
        raise KeyError(key)
    return REGISTRY[key]
