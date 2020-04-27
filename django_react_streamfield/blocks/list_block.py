from uuid import uuid4

from django import forms
from django.core.exceptions import ValidationError
from django.forms.utils import ErrorList
from django.utils.html import format_html, format_html_join

from ..exceptions import RemovedError
from ..widgets import BlockData
from .base import Block

__all__ = ["ListBlock"]


class ListBlock(Block):
    def __init__(self, child_block, **kwargs):
        Block.__init__(self, **kwargs)

        self.child_block = (
            child_block() if isinstance(child_block, type) else child_block
        )

        if not hasattr(self.meta, "default"):
            self.meta.default = [self.child_block.get_default()]

        self.dependencies = [self.child_block]

    @property
    def media(self):
        return forms.Media(
            js=[
                "django_react_streamfield/js/blocks/sequence.js",
                "django_react_streamfield/js/blocks/list.js",
            ]
        )

    def render_list_member(self, value, prefix, index, errors=None):
        raise RemovedError

    def html_declarations(self):
        raise RemovedError

    def js_initializer(self):
        raise RemovedError

    def render_form(self, value, prefix="", errors=None):
        raise RemovedError

    def value_from_datadict(self, data, files, prefix):
        return [
            self.child_block.value_from_datadict(child_block_data, files, prefix)
            for child_block_data in data["value"]
        ]

    def prepare_value(self, value, errors=None):
        children_errors = self.get_children_errors(errors)
        if children_errors is None:
            children_errors = [None] * len(value)
        prepared_value = []
        for child_value, child_errors in zip(value, children_errors):
            html = self.child_block.get_instance_html(child_value, errors=child_errors)
            child_value = BlockData(
                {
                    "id": str(uuid4()),
                    "type": self.child_block.name,
                    "hasError": bool(child_errors),
                    "value": self.child_block.prepare_value(
                        child_value, errors=child_errors
                    ),
                }
            )
            if html is not None:
                child_value["html"] = html
            prepared_value.append(child_value)
        return prepared_value

    def value_omitted_from_data(self, data, files, prefix):
        return ("%s-count" % prefix) not in data

    def clean(self, value):
        result = []
        errors = []
        for child_val in value:
            try:
                result.append(self.child_block.clean(child_val))
            except ValidationError as e:
                errors.append(ErrorList([e]))
            else:
                errors.append(None)

        if any(errors):
            # The message here is arbitrary - outputting error messages is delegated to the child blocks,
            # which only involves the 'params' list
            raise ValidationError("Validation error in ListBlock", params=errors)

        return result

    def to_python(self, value):
        # recursively call to_python on children and return as a list
        return [self.child_block.to_python(item) for item in value]

    def get_prep_value(self, value):
        # recursively call get_prep_value on children and return as a list
        return [self.child_block.get_prep_value(item) for item in value]

    def get_api_representation(self, value, context=None):
        # recursively call get_api_representation on children and return as a list
        return [
            self.child_block.get_api_representation(item, context=context)
            for item in value
        ]

    def render_basic(self, value, context=None):
        children = format_html_join(
            "\n",
            "<li>{0}</li>",
            [
                (self.child_block.render(child_value, context=context),)
                for child_value in value
            ],
        )
        return format_html("<ul>{0}</ul>", children)

    def get_searchable_content(self, value):
        content = []

        for child_value in value:
            content.extend(self.child_block.get_searchable_content(child_value))

        return content

    def check(self, **kwargs):
        errors = super().check(**kwargs)
        errors.extend(self.child_block.check(**kwargs))
        return errors

    class Meta:
        # No icon specified here, because that depends on the purpose that the
        # block is being used for. Feel encouraged to specify an icon in your
        # descendant block type
        icon = "placeholder"


DECONSTRUCT_ALIASES = {
    ListBlock: "django_react_streamfield.blocks.ListBlock",
}
