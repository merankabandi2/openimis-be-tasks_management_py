"""
Search tasks by the label of their GenericForeignKey entity, in SQL (OP-3332).

The label a user sees is `str(task.entity)`. Resolving it task by task costs one
query (or more) per task, so instead, for each entity type present among the
tasks, the filter adds a subquery on that type's table:

    entity_type = <ct> AND entity_id IN (SELECT pk::text FROM <model> WHERE <label> ILIKE '%text%')

The label of a type comes, in that order, from:
1. the model's own `label_expression()` classmethod (the core contract proposed
   in OP-3341);
2. LABEL_EXPRESSIONS below, for the entity types tasks use today;
3. the default `__str__` of Django or UUIDModel, when the model has none of its own.
A model whose `__str__` has none of these is still searched in Python, as before,
but only through the tasks of that type.

Once core provides `core.gfk.gfk_label_filter` (OP-3341), it is used instead.
"""
import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection, models
from django.db.models import Case, CharField, Func, Q, TextField, Value, When
from django.db.models.functions import Cast, Coalesce, Concat

from core.models import UUIDModel

logger = logging.getLogger(__name__)


def _text(field):
    """A field as Python's f-string prints it: NULL reads "None"."""
    return Coalesce(Cast(field, output_field=TextField()), Value("None"))


def _concat(*parts):
    """Strings are field names, Value() parts are literals."""
    return Concat(*(_text(p) if isinstance(p, str) else p for p in parts), output_field=TextField())


class _NaiveDatetimeText(Func):
    """`str()` of a naive datetime: microseconds are printed only when non-zero."""
    template = (
        "CASE WHEN to_char(%(expressions)s, 'US') = '000000' "
        "THEN to_char(%(expressions)s, 'YYYY-MM-DD HH24:MI:SS') "
        "ELSE to_char(%(expressions)s, 'YYYY-MM-DD HH24:MI:SS.US') END"
    )
    output_field = TextField()


def _datetime_text(field):
    """
    None when the database cannot print a datetime as Python does (another vendor,
    or aware datetimes whose text depends on the session time zone): the type is
    then searched in Python.
    """
    if connection.vendor != "postgresql" or settings.USE_TZ:
        return None
    return Coalesce(_NaiveDatetimeText(field), Value("None"))


def _user_label():
    # core.User.__str__: "(<type>) <username> [<id>]"
    user_type = Case(
        When(i_user__isnull=False, then=Value("i")),
        When(t_user__isnull=False, then=Value("t")),
        When(officer__isnull=False, then=Value("o")),
        When(claim_admin__isnull=False, then=Value("c")),
        default=Value("?"),
        output_field=TextField(),
    )
    return _concat(Value("("), user_type, Value(") "), "username", Value(" ["), "id", Value("]"))


def _upload_records_label(*prefix):
    # "<prefix> <source_name> <workflow> <date_created>"
    date_created = _datetime_text("date_created")
    if date_created is None:
        return None
    return _concat(*prefix, "data_upload__source_name", Value(" "), "workflow", Value(" "), date_created)


# The label of each entity type tasks point to, matching that model's __str__.
# Keyed by "app_label.model", so no other module is imported. Models without a
# __str__ of their own (individual.Group, individual.GroupIndividual) need no entry.
LABEL_EXPRESSIONS = {
    "core.user": _user_label,
    "individual.individual": lambda: _concat("first_name", Value(" "), "last_name"),
    "individual.individualdatauploadrecords": lambda: _upload_records_label(Value("Individual Import - ")),
    "social_protection.benefitplan": lambda: _concat(Value("Benefit Plan "), "code"),
    "social_protection.benefitplandatauploadrecords": lambda: _upload_records_label(
        "benefit_plan__code", Value(" ")),
    "payroll.payroll": lambda: _concat(Value("Payroll "), "name", Value(" - "), "id"),
}


def _default_label(model):
    """The label of a model that inherits Django's or UUIDModel's __str__, else None."""
    owner = next(klass for klass in model.__mro__ if "__str__" in vars(klass))
    if owner is models.Model:
        return _concat(Value(f"{model.__name__} object ("), "pk", Value(")"))
    if owner is UUIDModel:
        return _concat(Value("["), "id", Value("]"))
    return None


def label_expression(model):
    """The SQL expression of `str()` for instances of `model`, or None."""
    own = getattr(model, "label_expression", None)
    if callable(own):
        return own()
    build = LABEL_EXPRESSIONS.get(model._meta.label_lower)
    if build is not None:
        return build()
    return _default_label(model)


def label_queryset(content_type):
    """
    The rows of a content type annotated with `_label` and `_pk` (the key as text,
    as a GenericForeignKey stores it), or None when its label has no SQL
    expression. Uses the base manager, as the GenericForeignKey does.
    """
    model = content_type.model_class()
    expression = label_expression(model) if model is not None else None
    if expression is None:
        return None
    return model._base_manager.annotate(_label=expression, _pk=Cast("pk", output_field=CharField()))


def _matching_entity_ids_in_python(queryset, content_type, text):
    """Today's check, limited to the tasks of one type, with their entities loaded in bulk."""
    model = content_type.model_class()
    if model is None:
        return []
    logger.debug("tasks_management: no label expression for %s, entity search done in Python", content_type)
    keys = {}
    stored_ids = queryset.filter(entity_type_id=content_type.id).values_list("entity_id", flat=True).distinct()
    for entity_id in stored_ids.order_by():
        if not entity_id:
            continue
        try:
            keys[entity_id] = model._meta.pk.to_python(entity_id)
        except ValidationError:
            continue  # not a valid key for this model: no entity
    found = model._base_manager.in_bulk(set(keys.values()))
    text = text.lower()
    return [entity_id for entity_id, key in keys.items() if key in found and text in str(found[key]).lower()]


def filter_by_entity_label(queryset, text):
    """
    The tasks (or historical tasks) of `queryset` whose entity's label contains
    `text`, case-insensitively. A task without an entity never matches.
    """
    try:
        from core.gfk import gfk_label_filter
    except ImportError:
        pass
    else:
        return gfk_label_filter(queryset, "entity_type", "entity_id", text)

    condition = Q(pk__in=[])
    type_ids = queryset.order_by().values_list("entity_type", flat=True).distinct()
    for type_id in type_ids:
        if type_id is None:
            continue
        content_type = ContentType.objects.get_for_id(type_id)
        labels = label_queryset(content_type)
        if labels is not None:
            matching = labels.filter(_label__icontains=text).values("_pk")
        else:
            matching = _matching_entity_ids_in_python(queryset, content_type, text)
        condition |= Q(entity_type_id=type_id, entity_id__in=matching)
    return queryset.filter(condition)
