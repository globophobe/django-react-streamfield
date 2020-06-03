import collections
from importlib import import_module

from django import forms
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.template.loader import render_to_string
from django.utils.encoding import force_str
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe
from django.utils.text import capfirst
from django_react_streamfield.exceptions import RemovedError
from django_react_streamfield.widgets import get_non_block_errors

from ..widgets import BlockWidget

__all__ = [
    "BaseBlock",
    "Block",
    "BoundBlock",
    "DeclarativeSubBlocksMetaclass",
    "BlockWidget",
    "BlockField",
]


# =========================================
# Top-level superclasses and helper objects
# =========================================


class BaseBlock(type):
    def __new__(mcs, name, bases, attrs):
        meta_class = attrs.pop("Meta", None)

        cls = super(BaseBlock, mcs).__new__(mcs, name, bases, attrs)

        # Get all the Meta classes from all the bases
        meta_class_bases = [meta_class] + [
            getattr(base, "_meta_class", None) for base in bases
        ]
        meta_class_bases = tuple(filter(bool, meta_class_bases))
        cls._meta_class = type(str(name + "Meta"), meta_class_bases, {})

        return cls


class Block(metaclass=BaseBlock):
    FIELD_NAME_TEMPLATE = "field-__ID__"

    name = ""
    creation_counter = 0

    TEMPLATE_VAR = "value"

    class Meta:
        label = None
        icon = "placeholder"
        classname = None
        group = ""
        closed = False

    """
    Setting a 'dependencies' list serves as a shortcut for the common case where a complex block type
    (such as struct, list or stream) relies on one or more inner block objects, and needs to ensure that
    the responses from the 'media' and 'html_declarations' include the relevant declarations for those inner
    blocks, as well as its own. Specifying these inner block objects in a 'dependencies' list means that
    the base 'media' and 'html_declarations' methods will return those declarations; the outer block type can
    then add its own declarations to the list by overriding those methods and using super().
    """
    dependencies = []

    def __new__(cls, *args, **kwargs):
        # adapted from django.utils.deconstruct.deconstructible; capture the arguments
        # so that we can return them in the 'deconstruct' method
        obj = super(Block, cls).__new__(cls)
        obj._constructor_args = (args, kwargs)
        return obj

    def __init__(self, **kwargs):
        self.meta = self._meta_class()

        for attr, value in kwargs.items():
            setattr(self.meta, attr, value)

        # Increase the creation counter, and save our local copy.
        self.creation_counter = Block.creation_counter
        Block.creation_counter += 1
        self.definition_prefix = "blockdef-%d" % self.creation_counter

        self.label = self.meta.label or ""

    def get_default(self):
        default = self.meta.default
        if callable(default):
            default = default()
        return default

    def get_children_errors(self, errors):
        if errors:
            if len(errors) > 1:
                # We rely on Block.get_children_errors throwing
                # a single ValidationError with a specially crafted 'params'
                # attribute that we can pull apart and distribute
                # to the child blocks.
                raise TypeError(
                    "Block.get_children_errors unexpectedly "
                    "received multiple errors."
                )
            return errors.as_data()[0].params

    def prepare_value(self, value, errors=None):
        """
        Returns the value as it will be displayed in react-streamfield.
        """
        return value

    def get_instance_html(self, value, errors=None):
        """
        Returns the HTML template generated for a given value.

        That HTML will be displayed as the block content panel
        in react-streamfield. It is usually not rendered
        """
        help_text = getattr(self.meta, "help_text", None)
        non_block_errors = get_non_block_errors(errors)
        if help_text or non_block_errors:
            return render_to_string(
                "django_react_streamfield/block_forms/blocks_container.html",
                {"help_text": help_text, "non_block_errors": non_block_errors,},
            )

    @cached_property
    def definition(self):
        definition = {
            "key": self.name,
            "label": capfirst(self.label),
            "required": self.required,
            "closed": self.meta.closed,
            "dangerouslyRunInnerScripts": True,
        }
        if self.meta.icon != Block._meta_class.icon:
            definition["icon"] = '<i class="icon icon-%s"></i>' % self.meta.icon
        if self.meta.classname is not None:
            definition["className"] = self.meta.classname
        if self.meta.group:
            definition["group"] = str(self.meta.group)
        if self.meta.default:
            definition["default"] = self.prepare_value(self.get_default())
        return definition

    def all_blocks(self):
        """
        Return a list consisting of self and all block objects that are direct or indirect dependencies
        of this block
        """
        result = [self]
        for dep in self.dependencies:
            result.extend(dep.all_blocks())
        return result

    def all_media(self):
        media = forms.Media()

        # In cases where the same block definition appears multiple times within different
        # container blocks (e.g. a RichTextBlock appearing at the top level of a StreamField as
        # well as both sides of a StructBlock for producing two-column layouts), we will encounter
        # identical media declarations. Adding these to the final combined media declaration would
        # be redundant and add processing time when determining the final media ordering. To avoid
        # this, we keep a cache of previously-seen declarations and only add unique ones.
        media_cache = set()

        for block in self.all_blocks():
            key = block.media.__repr__()
            if key not in media_cache:
                media += block.media
                media_cache.add(key)
        return media

    def all_html_declarations(self):
        raise RemovedError

    def set_name(self, name):
        self.name = name
        if not self.meta.label:
            self.label = capfirst(force_str(name).replace("_", " "))

    @property
    def media(self):
        return forms.Media()

    def html_declarations(self):
        raise RemovedError

    def js_initializer(self):
        """
        Returns a Javascript expression string, or None if this block does not require any
        Javascript behaviour. This expression evaluates to an initializer function, a function that
        takes the ID prefix and applies JS behaviour to the block instance with that value and prefix.

        The parent block of this block (or the top-level page code) must ensure that this
        expression is not evaluated more than once. (The resulting initializer function can and will be
        called as many times as there are instances of this block, though.)
        """
        return None

    def render_form(self, value, prefix="", errors=None):
        """
        Render the HTML for this block with 'value' as its content.
        """
        raise NotImplementedError("%s.render_form" % self.__class__)

    def value_from_datadict(self, data, files, prefix):
        raise NotImplementedError("%s.value_from_datadict" % self.__class__)

    def value_omitted_from_data(self, data, files, name):
        """
        Used only for top-level blocks wrapped by BlockWidget (i.e.: typically only StreamBlock)
        to inform ModelForm logic on Django >=1.10.2 whether the field is absent from the form
        submission (and should therefore revert to the field default).
        """
        return name not in data

    def bind(self, value, prefix=None, errors=None):
        """
        Return a BoundBlock which represents the association of this block definition with a value
        and a prefix (and optionally, a ValidationError to be rendered).
        BoundBlock primarily exists as a convenience to allow rendering within templates:
        bound_block.render() rather than blockdef.render(value, prefix) which can't be called from
        within a template.
        """
        return BoundBlock(self, value, prefix=prefix, errors=errors)

    def get_default(self):
        """
        Return this block's default value (conventionally found in self.meta.default),
        converted to the value type expected by this block. This caters for the case
        where that value type is not something that can be expressed statically at
        model definition type (e.g. something like StructValue which incorporates a
        pointer back to the block definion object).
        """
        return self.meta.default

    def prototype_block(self):
        """
        Return a BoundBlock that can be used as a basis for new empty block instances to be added on the fly
        (new list items, for example). This will have a prefix of '__PREFIX__' (to be dynamically replaced with
        a real prefix when it's inserted into the page) and a value equal to the block's default value.
        """
        return self.bind(self.get_default(), "__PREFIX__")

    def clean(self, value):
        """
        Validate value and return a cleaned version of it, or throw a ValidationError if validation fails.
        The thrown ValidationError instance will subsequently be passed to render() to display the
        error message; the ValidationError must therefore include all detail necessary to perform that
        rendering, such as identifying the specific child block(s) with errors, in the case of nested
        blocks. (It is suggested that you use the 'params' attribute for this; using error_list /
        error_dict is unreliable because Django tends to hack around with these when nested.)
        """
        return value

    def to_python(self, value):
        """
        Convert 'value' from a simple (JSON-serialisable) value to a (possibly complex) Python value to be
        used in the rest of the block API and within front-end templates . In simple cases this might be
        the value itself; alternatively, it might be a 'smart' version of the value which behaves mostly
        like the original value but provides a native HTML rendering when inserted into a template; or it
        might be something totally different (e.g. an image chooser will use the image ID as the clean
        value, and turn this back into an actual image object here).
        """
        return value

    def get_prep_value(self, value):
        """
        The reverse of to_python; convert the python value into JSON-serialisable form.
        """
        return value

    def get_context(self, value, parent_context=None):
        """
        Return a dict of context variables (derived from the block value and combined with the parent_context)
        to be used as the template context when rendering this value through a template.
        """

        context = parent_context or {}
        context.update(
            {"self": value, self.TEMPLATE_VAR: value,}
        )
        return context

    def get_template(self, context=None):
        """
        Return the template to use for rendering the block if specified on meta class.
        This extraction was added to make dynamic templates possible if you override this method
        """
        return getattr(self.meta, "template", None)

    def render(self, value, context=None):
        """
        Return a text rendering of 'value', suitable for display on templates. By default, this will
        use a template (with the passed context, supplemented by the result of get_context) if a
        'template' property is specified on the block, and fall back on render_basic otherwise.
        """
        template = self.get_template(context=context)
        if not template:
            return self.render_basic(value, context=context)

        if context is None:
            new_context = self.get_context(value)
        else:
            new_context = self.get_context(value, parent_context=dict(context))

        return mark_safe(render_to_string(template, new_context))

    def get_api_representation(self, value, context=None):
        """
        Can be used to customise the API response and defaults to the value returned by get_prep_value.
        """
        return self.get_prep_value(value)

    def render_basic(self, value, context=None):
        """
        Return a text rendering of 'value', suitable for display on templates. render() will fall back on
        this if the block does not define a 'template' property.
        """
        return force_str(value)

    def get_searchable_content(self, value):
        """
        Returns a list of strings containing text content within this block to be used in a search engine.
        """
        return []

    def check(self, **kwargs):
        """
        Hook for the Django system checks framework -
        returns a list of django.core.checks.Error objects indicating validity errors in the block
        """
        return []

    def _check_name(self, **kwargs):
        """
        Helper method called by container blocks as part of the system checks framework,
        to validate that this block's name is a valid identifier.
        (Not called universally, because not all blocks need names)
        """
        errors = []
        if not self.name:
            errors.append(
                checks.Error(
                    "Block name %r is invalid" % self.name,
                    hint="Block name cannot be empty",
                    obj=kwargs.get("field", self),
                    id="wagtailcore.E001",
                )
            )

        if " " in self.name:
            errors.append(
                checks.Error(
                    "Block name %r is invalid" % self.name,
                    hint="Block names cannot contain spaces",
                    obj=kwargs.get("field", self),
                    id="wagtailcore.E001",
                )
            )

        if "-" in self.name:
            errors.append(
                checks.Error(
                    "Block name %r is invalid" % self.name,
                    "Block names cannot contain dashes",
                    obj=kwargs.get("field", self),
                    id="wagtailcore.E001",
                )
            )

        if self.name and self.name[0].isdigit():
            errors.append(
                checks.Error(
                    "Block name %r is invalid" % self.name,
                    "Block names cannot begin with a digit",
                    obj=kwargs.get("field", self),
                    id="wagtailcore.E001",
                )
            )

        return errors

    def id_for_label(self, prefix):
        """
        Return the ID to be used as the 'for' attribute of <label> elements that refer to this block,
        when the given field prefix is in use. Return None if no 'for' attribute should be used.
        """
        return None

    @property
    def required(self):
        """
        Flag used to determine whether labels for this block should display a 'required' asterisk.
        False by default, since Block does not provide any validation of its own - it's up to subclasses
        to define what required-ness means.
        """
        return False

    def deconstruct(self):
        # adapted from django.utils.deconstruct.deconstructible
        module_name = self.__module__
        name = self.__class__.__name__

        # Make sure it's actually there and not an inner class
        module = import_module(module_name)
        if not hasattr(module, name):
            raise ValueError(
                "Could not find object %s in %s.\n"
                "Please note that you cannot serialize things like inner "
                "classes. Please move the object into the main module "
                "body to use migrations.\n" % (name, module_name)
            )

        # if the module defines a DECONSTRUCT_ALIASES dictionary, see if the class has an entry in there;
        # if so, use that instead of the real path
        try:
            path = module.DECONSTRUCT_ALIASES[self.__class__]
        except (AttributeError, KeyError):
            path = "%s.%s" % (module_name, name)

        return (
            path,
            self._constructor_args[0],
            self._constructor_args[1],
        )

    def __eq__(self, other):
        """
        Implement equality on block objects so that two blocks with matching definitions are considered
        equal. (Block objects are intended to be immutable with the exception of set_name(), so here
        'matching definitions' means that both the 'name' property and the constructor args/kwargs - as
        captured in _constructor_args - are equal on both blocks.)

        This was originally necessary as a workaround for https://code.djangoproject.com/ticket/24340
        in Django <1.9; the deep_deconstruct function used to detect changes for migrations did not
        recurse into the block lists, and left them as Block instances. This __eq__ method therefore
        came into play when identifying changes within migrations.

        As of Django >=1.9, this *probably* isn't required any more. However, it may be useful in
        future as a way of identifying blocks that can be re-used within StreamField definitions
        (https://github.com/wagtail/wagtail/issues/4298#issuecomment-367656028).
        """

        if not isinstance(other, Block):
            # if the other object isn't a block at all, it clearly isn't equal.
            return False

            # Note that we do not require the two blocks to be of the exact same class. This is because
            # we may wish the following blocks to be considered equal:
            #
            # class FooBlock(StructBlock):
            #     first_name = CharBlock()
            #     surname = CharBlock()
            #
            # class BarBlock(StructBlock):
            #     first_name = CharBlock()
            #     surname = CharBlock()
            #
            # FooBlock() == BarBlock() == StructBlock([('first_name', CharBlock()), ('surname': CharBlock())])
            #
            # For this to work, StructBlock will need to ensure that 'deconstruct' returns the same signature
            # in all of these cases, including reporting StructBlock as the path:
            #
            # FooBlock().deconstruct() == (
            #     'wagtail.core.blocks.StructBlock',
            #     [('first_name', CharBlock()), ('surname': CharBlock())],
            #     {}
            # )
            #
            # This has the bonus side effect that the StructBlock field definition gets frozen into
            # the migration, rather than leaving the migration vulnerable to future changes to FooBlock / BarBlock
            # in models.py.

        return (self.name == other.name) and (self.deconstruct() == other.deconstruct())


class BoundBlock:
    def __init__(self, block, value, prefix=None, errors=None):
        self.block = block
        self.value = value
        self.prefix = prefix
        self.errors = errors

    def render_form(self):
        return self.block.render_form(self.value, self.prefix, errors=self.errors)

    def render(self, context=None):
        return self.block.render(self.value, context=context)

    def render_as_block(self, context=None):
        """
        Alias for render; the include_block tag will specifically check for the presence of a method
        with this name. (This is because {% include_block %} is just as likely to be invoked on a bare
        value as a BoundBlock. If we looked for a `render` method instead, we'd run the risk of finding
        an unrelated method that just happened to have that name - for example, when called on a
        PageChooserBlock it could end up calling page.render.
        """
        return self.block.render(self.value, context=context)

    def id_for_label(self):
        return self.block.id_for_label(self.prefix)

    def __str__(self):
        """Render the value according to the block's native rendering"""
        return self.block.render(self.value)


class DeclarativeSubBlocksMetaclass(BaseBlock):
    """
    Metaclass that collects sub-blocks declared on the base classes.
    (cheerfully stolen from https://github.com/django/django/blob/master/django/forms/forms.py)
    """

    def __new__(mcs, name, bases, attrs):
        # Collect sub-blocks declared on the current class.
        # These are available on the class as `declared_blocks`
        current_blocks = []
        for key, value in list(attrs.items()):
            if isinstance(value, Block):
                current_blocks.append((key, value))
                value.set_name(key)
                attrs.pop(key)
        current_blocks.sort(key=lambda x: x[1].creation_counter)
        attrs["declared_blocks"] = collections.OrderedDict(current_blocks)

        new_class = super(DeclarativeSubBlocksMetaclass, mcs).__new__(
            mcs, name, bases, attrs
        )

        # Walk through the MRO, collecting all inherited sub-blocks, to make
        # the combined `base_blocks`.
        base_blocks = collections.OrderedDict()
        for base in reversed(new_class.__mro__):
            # Collect sub-blocks from base class.
            if hasattr(base, "declared_blocks"):
                base_blocks.update(base.declared_blocks)

            # Field shadowing.
            for attr, value in base.__dict__.items():
                if value is None and attr in base_blocks:
                    base_blocks.pop(attr)
        new_class.base_blocks = base_blocks

        return new_class


# ========================
# django.forms integration
# ========================


class BlockField(forms.Field):
    """Wraps a block object as a form field so that it can be incorporated into a Django form"""

    def __init__(self, block=None, **kwargs):
        if block is None:
            raise ImproperlyConfigured("BlockField was not passed a 'block' object")
        self.block = block

        if "widget" not in kwargs:
            kwargs["widget"] = BlockWidget(block)

        super().__init__(**kwargs)

    def clean(self, value):
        return self.block.clean(value)


DECONSTRUCT_ALIASES = {
    Block: "django_react_streamfield.blocks.Block",
}