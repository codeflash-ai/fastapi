import dataclasses
import datetime
from collections import defaultdict, deque
from decimal import Decimal
from enum import Enum
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from pathlib import Path, PurePath
from re import Pattern
from types import GeneratorType
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union
from uuid import UUID

from fastapi.types import IncEx
from pydantic import BaseModel
from pydantic.color import Color
from pydantic.networks import AnyUrl, NameEmail
from pydantic.types import SecretBytes, SecretStr
from typing_extensions import Annotated, Doc

from ._compat import PYDANTIC_V2, UndefinedType, Url, _model_dump


# Taken from Pydantic v1 as is
def isoformat(o: Union[datetime.date, datetime.time]) -> str:
    return o.isoformat()


# Taken from Pydantic v1 as is
# TODO: pv2 should this return strings instead?
def decimal_encoder(dec_value: Decimal) -> Union[int, float]:
    """
    Encodes a Decimal as int of there's no exponent, otherwise float

    This is useful when we use ConstrainedDecimal to represent Numeric(x,0)
    where a integer (but not int typed) is used. Encoding this as a float
    results in failed round-tripping between encode and parse.
    Our Id type is a prime example of this.

    >>> decimal_encoder(Decimal("1.0"))
    1.0

    >>> decimal_encoder(Decimal("1"))
    1
    """
    if dec_value.as_tuple().exponent >= 0:  # type: ignore[operator]
        return int(dec_value)
    else:
        return float(dec_value)


ENCODERS_BY_TYPE: Dict[Type[Any], Callable[[Any], Any]] = {
    bytes: lambda o: o.decode(),
    Color: str,
    datetime.date: isoformat,
    datetime.datetime: isoformat,
    datetime.time: isoformat,
    datetime.timedelta: lambda td: td.total_seconds(),
    Decimal: decimal_encoder,
    Enum: lambda o: o.value,
    frozenset: list,
    deque: list,
    GeneratorType: list,
    IPv4Address: str,
    IPv4Interface: str,
    IPv4Network: str,
    IPv6Address: str,
    IPv6Interface: str,
    IPv6Network: str,
    NameEmail: str,
    Path: str,
    Pattern: lambda o: o.pattern,
    SecretBytes: str,
    SecretStr: str,
    set: list,
    UUID: str,
    Url: str,
    AnyUrl: str,
}


def generate_encoders_by_class_tuples(
    type_encoder_map: Dict[Any, Callable[[Any], Any]],
) -> Dict[Callable[[Any], Any], Tuple[Any, ...]]:
    encoders_by_class_tuples: Dict[Callable[[Any], Any], Tuple[Any, ...]] = defaultdict(
        tuple
    )
    for type_, encoder in type_encoder_map.items():
        encoders_by_class_tuples[encoder] += (type_,)
    return encoders_by_class_tuples


encoders_by_class_tuples = generate_encoders_by_class_tuples(ENCODERS_BY_TYPE)


def jsonable_encoder(
    obj: Annotated[
        Any,
        Doc(
            """
            The input object to convert to JSON.
            """
        ),
    ],
    include: Annotated[
        Optional[IncEx],
        Doc(
            """
            Pydantic's `include` parameter, passed to Pydantic models to set the
            fields to include.
            """
        ),
    ] = None,
    exclude: Annotated[
        Optional[IncEx],
        Doc(
            """
            Pydantic's `exclude` parameter, passed to Pydantic models to set the
            fields to exclude.
            """
        ),
    ] = None,
    by_alias: Annotated[
        bool,
        Doc(
            """
            Pydantic's `by_alias` parameter, passed to Pydantic models to define if
            the output should use the alias names (when provided) or the Python
            attribute names. In an API, if you set an alias, it's probably because you
            want to use it in the result, so you probably want to leave this set to
            `True`.
            """
        ),
    ] = True,
    exclude_unset: Annotated[
        bool,
        Doc(
            """
            Pydantic's `exclude_unset` parameter, passed to Pydantic models to define
            if it should exclude from the output the fields that were not explicitly
            set (and that only had their default values).
            """
        ),
    ] = False,
    exclude_defaults: Annotated[
        bool,
        Doc(
            """
            Pydantic's `exclude_defaults` parameter, passed to Pydantic models to define
            if it should exclude from the output the fields that had the same default
            value, even when they were explicitly set.
            """
        ),
    ] = False,
    exclude_none: Annotated[
        bool,
        Doc(
            """
            Pydantic's `exclude_none` parameter, passed to Pydantic models to define
            if it should exclude from the output any fields that have a `None` value.
            """
        ),
    ] = False,
    custom_encoder: Annotated[
        Optional[Dict[Any, Callable[[Any], Any]]],
        Doc(
            """
            Pydantic's `custom_encoder` parameter, passed to Pydantic models to define
            a custom encoder.
            """
        ),
    ] = None,
    sqlalchemy_safe: Annotated[
        bool,
        Doc(
            """
            Exclude from the output any fields that start with the name `_sa`.

            This is mainly a hack for compatibility with SQLAlchemy objects, they
            store internal SQLAlchemy-specific state in attributes named with `_sa`,
            and those objects can't (and shouldn't be) serialized to JSON.
            """
        ),
    ] = True,
) -> Any:
    """
    Convert any object to something that can be encoded in JSON.

    This is used internally by FastAPI to make sure anything you return can be
    encoded as JSON before it is sent to the client.

    You can also use it yourself, for example to convert objects before saving them
    in a database that supports only JSON.

    Read more about it in the
    [FastAPI docs for JSON Compatible Encoder](https://fastapi.tiangolo.com/tutorial/encoder/).
    """
    # Fast path for None and primitive JSON types
    if obj is None or isinstance(obj, (str, int, float)):
        return obj
    obj_type = type(obj)
    # Fast custom encoder by concrete type
    if custom_encoder:
        encoder = custom_encoder.get(obj_type, None)
        if encoder is not None:
            return encoder(obj)
        # Fallback: isinstance check for all (type, encoder) pairs
        for encoder_type, encoder_instance in custom_encoder.items():
            if isinstance(obj, encoder_type):
                return encoder_instance(obj)
    # Normalize include/exclude eagerly once per call
    use_include = include
    use_exclude = exclude
    if use_include is not None and not isinstance(use_include, (set, dict)):
        use_include = set(use_include)
    if use_exclude is not None and not isinstance(use_exclude, (set, dict)):
        use_exclude = set(use_exclude)

    # Short-circuit for BaseModel
    if isinstance(obj, BaseModel):
        # TODO: remove when deprecating Pydantic v1
        encoders: Dict[Any, Any] = {}
        if not PYDANTIC_V2:
            encoders = getattr(obj.__config__, "json_encoders", {})  # type: ignore[attr-defined]
            if custom_encoder:
                encoders.update(custom_encoder)
        obj_dict = _model_dump(
            obj,
            mode="json",
            include=use_include,
            exclude=use_exclude,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_none=exclude_none,
            exclude_defaults=exclude_defaults,
        )
        if "__root__" in obj_dict:
            obj_dict = obj_dict["__root__"]
        return jsonable_encoder(
            obj_dict,
            exclude_none=exclude_none,
            exclude_defaults=exclude_defaults,
            # TODO: remove when deprecating Pydantic v1
            custom_encoder=encoders,
            sqlalchemy_safe=sqlalchemy_safe,
        )
    # Short-circuit for dataclasses
    if dataclasses.is_dataclass(obj):
        obj_dict = dataclasses.asdict(obj)
        return jsonable_encoder(
            obj_dict,
            include=use_include,
            exclude=use_exclude,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            custom_encoder=custom_encoder,
            sqlalchemy_safe=sqlalchemy_safe,
        )

    # Short-circuit for Enum
    if isinstance(obj, Enum):
        return obj.value
    # Short-circuit for PurePath
    if isinstance(obj, PurePath):
        return str(obj)
    # Short-circuit for UndefinedType
    if isinstance(obj, UndefinedType):
        return None

    # Optimized dict handling
    if isinstance(obj, dict):
        if (
            use_include is None
            and use_exclude is None
            and not sqlalchemy_safe
            and not exclude_none
        ):
            # Fast path: Just encode values if no filters/exclusions are present (common in FastAPI payloads)
            return {
                k: jsonable_encoder(
                    v,
                    by_alias=by_alias,
                    exclude_unset=exclude_unset,
                    exclude_defaults=exclude_defaults,
                    exclude_none=exclude_none,
                    custom_encoder=custom_encoder,
                    sqlalchemy_safe=sqlalchemy_safe,
                )
                for k, v in obj.items()
            }
        allowed_keys = set(obj.keys())
        if use_include is not None:
            allowed_keys &= set(use_include)
        if use_exclude is not None:
            allowed_keys -= set(use_exclude)
        out = {}
        try:
            # Speed: avoid function attr lookups inside hot loop
            _jsonable_encoder = jsonable_encoder
            for key, value in obj.items():
                key_allowed = (
                    (
                        not sqlalchemy_safe
                        or (not isinstance(key, str))
                        or (not key.startswith("_sa"))
                    )
                    and (value is not None or not exclude_none)
                    and key in allowed_keys
                )
                if key_allowed:
                    # Optimize: Only recursively encode key if not a string (JSON allows only str keys)
                    if isinstance(key, str):
                        encoded_key = key
                    else:
                        encoded_key = _jsonable_encoder(
                            key,
                            by_alias=by_alias,
                            exclude_unset=exclude_unset,
                            exclude_none=exclude_none,
                            custom_encoder=custom_encoder,
                            sqlalchemy_safe=sqlalchemy_safe,
                        )
                    encoded_value = _jsonable_encoder(
                        value,
                        by_alias=by_alias,
                        exclude_unset=exclude_unset,
                        exclude_defaults=exclude_defaults,
                        exclude_none=exclude_none,
                        custom_encoder=custom_encoder,
                        sqlalchemy_safe=sqlalchemy_safe,
                    )
                    out[encoded_key] = encoded_value
            return out
        except Exception:
            raise
    # Optimized list/set/frozenset/tuple/GeneratorType/deque handling using list comprehension
    iterable_types = (list, set, frozenset, GeneratorType, tuple, deque)
    if isinstance(obj, iterable_types):
        # Avoid named function lookup inside comprehension by storing reference
        _jsonable_encoder = jsonable_encoder
        return [
            _jsonable_encoder(
                item,
                include=use_include,
                exclude=use_exclude,
                by_alias=by_alias,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
                custom_encoder=custom_encoder,
                sqlalchemy_safe=sqlalchemy_safe,
            )
            for item in obj
        ]
    # Fast-path builtin encoder registry
    encoder_func = ENCODERS_BY_TYPE.get(obj_type)
    if encoder_func is not None:
        return encoder_func(obj)
    # Fallback: encoder by tuple-of-types in registry, single dispatch via instance checking
    for encoder, classes_tuple in encoders_by_class_tuples.items():
        if isinstance(obj, classes_tuple):
            return encoder(obj)

    # Try dict(obj), then vars(obj), finally raise with both errors
    try:
        data = dict(obj)
    except Exception as e:
        errors: List[Exception] = [e]
        try:
            data = vars(obj)
        except Exception as e2:
            errors.append(e2)
            raise ValueError(errors) from e2
    return jsonable_encoder(
        data,
        include=use_include,
        exclude=use_exclude,
        by_alias=by_alias,
        exclude_unset=exclude_unset,
        exclude_defaults=exclude_defaults,
        exclude_none=exclude_none,
        custom_encoder=custom_encoder,
        sqlalchemy_safe=sqlalchemy_safe,
    )
