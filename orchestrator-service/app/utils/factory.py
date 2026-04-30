"""
app/utils/factory.py
──────────────────────
IFactory mixin: a lightweight registry + create() factory pattern.

Each subclass that wants to be discoverable by name registers itself
by calling cls.register(name) from its module-level code, or by
inheriting from IFactory (auto-registration on class definition).

BaseSink inherits IFactory so that:
    BaseSink.create("mongo", rule_config=..., tenant_id=...) → MongoSink(...)
"""
from __future__ import annotations

import logging
from abc import ABCMeta
from typing import Any, ClassVar, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="IFactory")


class IFactory(metaclass=ABCMeta):
    """
    Mixin that provides a class registry and create() factory method.

    Subclasses are registered automatically when they are defined
    (via __init_subclass__) if they have a non-None ``name`` class attribute.
    """

    _registry: ClassVar[dict[str, Type["IFactory"]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        name: str | None = getattr(cls, "name", None)
        if name and isinstance(name, str):
            IFactory._registry[name.lower()] = cls
            logger.debug("IFactory registered: %s → %s", name, cls.__qualname__)

    @classmethod
    def create(cls: Type[T], name: str, **kwargs: Any) -> T:
        """
        Instantiate a registered subclass by name.

        Raises ValueError for unknown names.
        """
        key = name.lower().replace("-", "_")
        klass = cls._registry.get(key)
        if klass is None:
            raise ValueError(
                f"No IFactory subclass registered for {name!r}. "
                f"Known: {sorted(cls._registry)}"
            )
        return klass(**kwargs)  # type: ignore[return-value]
