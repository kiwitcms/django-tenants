import re
import warnings
from django.conf import settings
from importlib import import_module

from django.utils.module_loading import import_string

from django_tenants.postgresql_backend.introspection import DatabaseSchemaIntrospection
from django_tenants.utils import get_public_schema_name, get_limit_set_calls, quote_schema_name
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured, ValidationError
import django.db.utils

import psycopg
from psycopg import sql



DatabaseError = django.db.utils.DatabaseError
IntegrityError = psycopg.IntegrityError

ORIGINAL_BACKEND = getattr(settings, 'ORIGINAL_BACKEND', 'django.db.backends.postgresql')

original_backend = import_module(ORIGINAL_BACKEND + '.base')

EXTRA_SEARCH_PATHS = getattr(settings, 'PG_EXTRA_SEARCH_PATHS', [])

EXTRA_SET_TENANT_METHOD_PATH = getattr(settings, 'EXTRA_SET_TENANT_METHOD_PATH', None)
if EXTRA_SET_TENANT_METHOD_PATH:
    EXTRA_SET_TENANT_METHOD = import_string(EXTRA_SET_TENANT_METHOD_PATH)
else:
    EXTRA_SET_TENANT_METHOD = None

# Valid PostgreSQL schema name regex
# Criteria:
#  1. Cannot start with 'pg_'
#  2. Can be any valid character, if quoted -- except quotes, backslashes,
#     line breaks,
#     semicolons and NUL. Schema names are always quoted when they reach SQL,
#     but they are also interpolated into the clone_schema() SQL function, so
#     reject anything that could break out of a quoted name or end a statement.
#  3. Cannot contain '/' and cannot be '.' or '..'. The schema name is also used
#     as a directory name for tenant media, static files and templates (see
#     parse_tenant_config_path), so it must not be able to traverse paths.
#  4. Must be between 1 and 63 characters long, and at most 63 bytes once
#     encoded as UTF-8 (checked in is_valid_schema_name). PostgreSQL silently
#     truncates identifiers to 63 bytes, so a longer name made of multi-byte
#     characters would become another tenant's schema name.
#
# Reference:
# https://www.postgresql.org/docs/13/sql-createschema.html
PGSQL_VALID_SCHEMA_NAME = re.compile(r'^(?!pg_)(?!\.{1,2}\Z)[^"\'\\/;\x00\r\n]{1,63}\Z', re.IGNORECASE)


# PostgreSQL's NAMEDATALEN - 1. Identifiers longer than this are truncated.
PGSQL_MAX_IDENTIFIER_BYTES = 63


def is_valid_schema_name(name):
    if not PGSQL_VALID_SCHEMA_NAME.match(name):
        return False
    try:
        return len(name.encode('utf-8')) <= PGSQL_MAX_IDENTIFIER_BYTES
    except UnicodeEncodeError:
        # e.g. a lone surrogate, which PostgreSQL could not store either
        return False


def _check_schema_name(name):
    if not is_valid_schema_name(name):
        raise ValidationError("Invalid string used for the schema name.")


class DatabaseWrapper(original_backend.DatabaseWrapper):
    """
    Adds the capability to manipulate the search_path using set_tenant and set_schema_name
    """
    include_public_schema = True
    # Use a patched version of the DatabaseIntrospection that only returns the table list for the
    # currently selected schema.

    def __init__(self, *args, **kwargs):
        self.search_path_set_schemas = None
        self.tenant = None
        self.schema_name = None
        super().__init__(*args, **kwargs)

        # Use a patched version of the DatabaseIntrospection that only returns the table list for the
        # currently selected schema.
        self.introspection = DatabaseSchemaIntrospection(self)

        self.set_schema_to_public()

    def close(self):
        self.search_path_set_schemas = None
        super().close()

    def set_tenant(self, tenant, include_public=True):
        """
        Main API method to current database schema,
        but it does not actually modify the db connection.
        """
        self.tenant = tenant
        self.schema_name = tenant.schema_name
        self.include_public_schema = include_public
        self.set_settings_schema(self.schema_name)

        if EXTRA_SET_TENANT_METHOD:
            EXTRA_SET_TENANT_METHOD(self, tenant)

        self.search_path_set_schemas = None

        # Content type can no longer be cached as public and tenant schemas
        # have different models. If someone wants to change this, the cache
        # needs to be separated between public and shared schemas. If this
        # cache isn't cleared, this can cause permission problems. For example,
        # on public, a particular model has id 14, but on the tenants it has
        # the id 15. if 14 is cached instead of 15, the permissions for the
        # wrong model will be fetched.
        ContentType.objects.clear_cache()

    def set_schema(self, schema_name, include_public=True):
        """
        Main API method to current database schema,
        but it does not actually modify the db connection.
        """
        self.set_tenant(FakeTenant(schema_name=schema_name), include_public)

    def set_schema_to_public(self):
        """
        Instructs to stay in the common 'public' schema.
        """
        self.set_tenant(FakeTenant(schema_name=get_public_schema_name()))

    def set_settings_schema(self, schema_name):
        self.settings_dict['SCHEMA'] = schema_name

    def get_schema(self):
        warnings.warn("connection.get_schema() is deprecated, use connection.schema_name instead.",
                      category=DeprecationWarning)
        return self.schema_name

    def get_tenant(self):
        warnings.warn("connection.get_tenant() is deprecated, use connection.tenant instead.",
                      category=DeprecationWarning)
        return self.tenant

    def _cursor(self, name=None):
        """
        Here it happens. We hope every Django db operation using PostgreSQL
        must go through this to get the cursor handle. We change the path.
        """
        if name:
            # Only supported and required by Django 1.11 (server-side cursor)
            cursor = super()._cursor(name=name)
        else:
            cursor = super()._cursor()

        # optionally limit the number of executions - under load, the execution
        # of `set search_path` can be quite time consuming
        if (not get_limit_set_calls()) or not self.search_path_set_schemas:
            # Actual search_path modification for the cursor. Database will
            # search schemata from left to right when looking for the object
            # (table, index, sequence, etc.).
            if not self.schema_name:
                raise ImproperlyConfigured("Database schema not set. Did you forget "
                                           "to call set_schema() or set_tenant()?")

            search_paths = self._get_cursor_search_paths()

            # Named cursor can only be used once and Django 4 has a recursion
            # issue, so always open a separate cursor for the search path.
            cursor_for_search_path = self.connection.cursor()

            # In the event that an error already happened in this transaction and we are going
            # to rollback we should just ignore database error when setting the search_path
            # if the next instruction is not a rollback it will just fail also, so
            # we do not have to worry that it's not the good one
            try:
                cursor_for_search_path.execute(
                    sql.SQL('SET search_path = {0}').format(
                        sql.SQL(', ').join(
                            quote_schema_name(name) for name in search_paths
                        )
                    )
                )
            except (django.db.utils.DatabaseError, psycopg.InternalError):
                self.search_path_set_schemas = None
            else:
                self.search_path_set_schemas = search_paths
            cursor_for_search_path.close()
        return cursor

    def _get_cursor_search_paths(self):
        _check_schema_name(self.schema_name)
        public_schema_name = get_public_schema_name()

        if self.schema_name == public_schema_name:
            search_paths = [public_schema_name]
        elif self.include_public_schema:
            search_paths = [self.schema_name, public_schema_name]
        else:
            search_paths = [self.schema_name]

        search_paths.extend(EXTRA_SEARCH_PATHS)

        return search_paths


class FakeTenant:
    """
    We can't import any db model in a backend (apparently?), so this class is used
    for wrapping schema names in a tenant-like structure.
    """
    def __init__(self, schema_name):
        self.schema_name = schema_name
