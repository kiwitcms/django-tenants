from django.conf import settings
from django.template import Library
from django.template.defaulttags import URLNode
from django.template.defaulttags import url as default_url
from django_tenants.utils import clean_tenant_url, get_public_schema_name, get_app_label

register = Library()


class SchemaURLNode(URLNode):
    def __init__(self, url_node):
        super().__init__(url_node.view_name, url_node.args, url_node.kwargs, url_node.asvar)

    def render(self, context):
        url = super().render(context)
        return clean_tenant_url(url)


@register.tag
def url(parser, token):
    return SchemaURLNode(default_url(parser, token))


@register.simple_tag
def public_schema():
    return get_public_schema_name()


@register.simple_tag(takes_context=True)
def is_tenant_app(context, app):
    _apps = settings.TENANT_APPS

    return app["app_label"] in [get_app_label(_app) for _app in _apps]


@register.simple_tag()
def is_shared_app(app):
    _apps = settings.SHARED_APPS

    return app["app_label"] in [get_app_label(_app) for _app in _apps]


@register.simple_tag()
def colour_admin_apps():
    if hasattr(settings, 'TENANT_COLOR_ADMIN_APPS'):
        return settings.TENANT_COLOR_ADMIN_APPS
    return True


@register.simple_tag(takes_context=True)
def is_public_schema(context, app):
    return not hasattr(context.request, 'tenant') or context.request.tenant.schema_name == get_public_schema_name()
