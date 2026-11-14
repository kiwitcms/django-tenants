from contextlib import contextmanager
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test.utils import override_settings
from django.utils.version import get_main_version, get_version_tuple

from psycopg import sql

from django_tenants.clone import CloneSchema
from django_tenants.signals import post_schema_sync, schema_migrated, schema_migrate_message, schema_pre_migration
from dts_test_app.models import DummyModel, ModelWithFkToPublicUser

from django_tenants.migration_executors import get_executor
from django_tenants.test.cases import TenantTestCase
from django_tenants.tests.testcases import BaseTestCase
from django_tenants.utils import tenant_context, schema_context, schema_exists, get_tenant_model, \
    get_public_schema_name, get_tenant_domain_model, schema_rename, quote_schema_name


@contextmanager
def catch_signal(signal):
    """
    Catch django signal and return the mocked call.
    """
    handler = mock.Mock()
    signal.connect(handler)
    yield handler
    signal.disconnect(handler)


class TenantDataAndSettingsTest(BaseTestCase):
    """
    Tests if the tenant model settings work properly and if data can be saved
    and persisted to different tenants.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        settings.SHARED_APPS = ('django_tenants',
                                'customers')
        settings.TENANT_APPS = ('dts_test_app',
                                'django.contrib.contenttypes',
                                'django.contrib.auth', )
        settings.INSTALLED_APPS = settings.SHARED_APPS + settings.TENANT_APPS
        cls.sync_shared()

        cls.public_tenant = get_tenant_model()(schema_name=get_public_schema_name())
        cls.public_tenant.save()
        cls.public_domain = get_tenant_domain_model()(tenant=cls.public_tenant, domain='test.com')
        cls.public_domain.save()

    @classmethod
    def tearDownClass(cls):
        cls.public_domain.delete()
        cls.public_tenant.delete()

        super().tearDownClass()

    def setUp(self):
        self.created = []

        super().setUp()

    def tearDown(self):
        from django_tenants.models import TenantMixin

        connection.set_schema_to_public()

        for c in self.created:
            if isinstance(c, TenantMixin):
                c.delete(force_drop=True)
            else:
                c.delete()

        super().tearDown()

    def test_tenant_schema_is_created(self):
        """
        When saving a tenant, it's schema should be created.
        """
        tenant = get_tenant_model()(schema_name='test')
        tenant.save()

        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()

        self.assertTrue(schema_exists(tenant.schema_name))

        self.created = [domain, tenant]

    def test_tenant_schema_is_created_atomically(self):
        """
        When saving a tenant, it's schema should be created.
        This should work in atomic transactions too.
        """
        executor = get_executor()
        Tenant = get_tenant_model()

        schema_name = 'test'

        @transaction.atomic()
        def atomically_create_tenant():
            t = Tenant(schema_name=schema_name)
            t.save()

            self.created = [t]

        if executor == 'simple':
            atomically_create_tenant()

            self.assertTrue(schema_exists(schema_name))
        elif executor == 'multiprocessing':
            # Unfortunately, it's impossible for the multiprocessing executor
            # to assert atomic transactions when creating a tenant
            with self.assertRaises(transaction.TransactionManagementError):
                atomically_create_tenant()

    def test_non_auto_sync_tenant(self):
        """
        When saving a tenant that has the flag auto_create_schema as
        False, the schema should not be created when saving the tenant.
        """

        tenant = get_tenant_model()(schema_name='test')
        tenant.auto_create_schema = False
        tenant.save()

        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()

        self.assertFalse(schema_exists(tenant.schema_name))

        self.created = [domain, tenant]

    def test_sync_tenant(self):
        """
        When editing an existing tenant, all data should be kept.
        """
        tenant = get_tenant_model()(schema_name='test')
        tenant.save()

        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()

        # go to tenant's path
        connection.set_tenant(tenant)

        # add some data
        DummyModel(name="Schemas are").save()
        DummyModel(name="awesome!").save()

        # edit tenant
        connection.set_schema_to_public()
        tenant.domain_urls = ['example.com']
        tenant.save()

        connection.set_tenant(tenant)

        # test if data is still there
        self.assertEqual(DummyModel.objects.count(), 2)

        self.created = [domain, tenant]

    def test_switching_search_path(self):
        """
        IMPORTANT: using schema_name with underscore here. See
        https://github.com/django-tenants/django-tenants/pull/829
        """
        tenant1 = get_tenant_model()(schema_name='tenant`1')
        tenant1.save()

        domain1 = get_tenant_domain_model()(tenant=tenant1, domain='something.test.com')
        domain1.save()

        connection.set_schema_to_public()

        tenant2 = get_tenant_model()(schema_name='Tenant_2')
        tenant2.save()

        domain2 = get_tenant_domain_model()(tenant=tenant2, domain='example.com')
        domain2.save()

        # go to tenant1's path
        connection.set_tenant(tenant1)

        # add some data, 2 DummyModels for tenant1
        DummyModel(name="Schemas are").save()
        DummyModel(name="awesome!").save()

        # switch temporarily to tenant2's path
        with self.assertNumQueries(3):
            with tenant_context(tenant2):
                # add some data, 3 DummyModels for tenant2
                DummyModel(name="Man,").save()
                DummyModel(name="testing").save()
                DummyModel(name="is great!").save()

        # we should be back to tenant1's path, test what we have
        with self.assertNumQueries(1):
            self.assertEqual(2, DummyModel.objects.count())

        # switch back to tenant2's path
        with self.assertNumQueries(1):
            with tenant_context(tenant2):
                self.assertEqual(3, DummyModel.objects.count())

        self.created = [domain2, domain1, tenant2, tenant1]

    @override_settings(TENANT_LIMIT_SET_CALLS=True)
    def test_switching_search_path_limited_calls(self):
        tenant1 = get_tenant_model()(schema_name='tenant1')
        tenant1.save()

        domain1 = get_tenant_domain_model()(tenant=tenant1, domain='something.test.com')
        domain1.save()

        connection.set_schema_to_public()

        tenant2 = get_tenant_model()(schema_name='tenant2')
        tenant2.save()

        domain2 = get_tenant_domain_model()(tenant=tenant2, domain='example.com')
        domain2.save()

        # set path is not executed when setting tenant so 0 queries expected
        with self.assertNumQueries(0):
            connection.set_tenant(tenant1)

        # switch temporarily to tenant2's path
        with self.assertNumQueries(3):
            with tenant_context(tenant2):
                DummyModel(name="Man,").save()
                DummyModel(name="testing").save()
                DummyModel(name="is great!").save()

        # 0 queries as search path not set here
        with self.assertNumQueries(0):
            connection.set_tenant(tenant1)

        with self.assertNumQueries(1):
            self.assertEqual(0, DummyModel.objects.count())

        # 0 queries as search path not set here
        with self.assertNumQueries(0):
            connection.set_tenant(tenant2)

        with self.assertNumQueries(1):
            self.assertEqual(3, DummyModel.objects.count())

        self.created = [domain2, domain1, tenant2, tenant1]

    def test_switching_tenant_without_previous_tenant(self):
        tenant = get_tenant_model()(schema_name='test')
        tenant.save()

        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()

        connection.tenant = None
        with tenant_context(tenant):
            DummyModel(name="No exception please").save()

        connection.tenant = None
        with schema_context(tenant.schema_name):
            DummyModel(name="Survived it!").save()

        self.created = [domain, tenant]

    def test_tenant_schema_behaves_as_decorator(self):
        tenant = get_tenant_model()(schema_name='test')
        tenant.save()

        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()

        connection.tenant = None

        @tenant_context(tenant)
        def test_tenant_func():
            DummyModel(name='No exception please').save()

        test_tenant_func()

        connection.tenant = None

        @schema_context(tenant.schema_name)
        def test_schema_func():
            DummyModel(name='Survived it!').save()

        test_schema_func()

        self.created = [domain, tenant]

    def test_tenant_schema_creation_with_sql_keyword_name(self):
        tenant = get_tenant_model()(schema_name='select')
        tenant.save()

        self.assertTrue(schema_exists(tenant.schema_name))

        self.created = [tenant]

    def test_tenant_schema_creation_with_only_numbers_name(self):
        tenant = get_tenant_model()(schema_name='123')
        tenant.save()

        self.assertTrue(schema_exists(tenant.schema_name))

        self.created = [tenant]

    def test_tenant_schema_creation_with_special_chars(self):
        """Tests using special characters in schema name."""
        schema_names = ('test-hyphen', 'test@at', 'test`backtick', 'country_BD')

        Client = get_tenant_model()
        for schema_name in schema_names:
            tenant = Client(schema_name=schema_name)
            tenant.save()
            self.assertTrue(schema_exists(tenant.schema_name))


class BaseSyncTest(BaseTestCase):
    """
    Tests if the shared apps and the tenant apps get synced correctly
    depending on if the public schema or a tenant is being synced.
    """
    MIGRATION_TABLE_SIZE = 1

    SHARED_APPS = ('django_tenants',  # 2 tables
                   'customers',
                   'django.contrib.auth',  # 6 tables
                   'django.contrib.contenttypes', )  # 1 table
    TENANT_APPS = ('django.contrib.sessions', )

    def setUp(self):
        super().setUp()
        # Django calls syncdb by default for the test database, but we want
        # a blank public schema for this set of tests.
        connection.set_schema_to_public()
        with connection.cursor() as cursor:
            cursor.execute('DROP SCHEMA %s CASCADE; CREATE SCHEMA %s;'
                           % (get_public_schema_name(), get_public_schema_name(), ))

        self.sync_shared()


class TenantSyncTest(BaseSyncTest):
    def test_shared_apps_does_not_sync_tenant_apps(self):
        """
        Tests that if an app is in SHARED_APPS, it does not get synced to
        the a tenant schema.
        """
        shared_tables = self.get_tables_list_in_schema(get_public_schema_name())
        self.assertEqual(2 + 6 + 1 + self.MIGRATION_TABLE_SIZE, len(shared_tables))
        self.assertNotIn('django_session', shared_tables)

    def test_tenant_apps_does_not_sync_shared_apps(self):
        """
        Tests that if an app is in TENANT_APPS, it does not get synced to
        the public schema.
        """
        tenant = get_tenant_model()(schema_name='test')
        tenant.save()

        domain = get_tenant_domain_model()(tenant=tenant, domain='arbitrary.test.com')
        domain.save()

        tenant_tables = self.get_tables_list_in_schema(tenant.schema_name)
        self.assertEqual(1 + self.MIGRATION_TABLE_SIZE, len(tenant_tables))
        self.assertIn('django_session', tenant_tables)

        connection.set_schema_to_public()
        domain.delete()
        tenant.delete(force_drop=True)


class TestSyncTenantsWithAuth(BaseSyncTest):
    SHARED_APPS = ('django_tenants',  # 2 tables
                   'customers',
                   'django.contrib.auth',  # 6 tables
                   'django.contrib.contenttypes',  # 1 table
                   'django.contrib.sessions', )  # 1 table
    TENANT_APPS = ('django.contrib.sessions', )  # 1 table

    if get_version_tuple(get_main_version()) < (5, 2):
        def _pre_setup(self):
            self.sync_shared()
            super()._pre_setup()
    else:
        @classmethod
        def _pre_setup(cls):
            cls.sync_shared()
            super()._pre_setup()

    def test_tenant_apps_and_shared_apps_can_have_the_same_apps(self):
        """
        Tests that both SHARED_APPS and TENANT_APPS can have apps in common.
        In this case they should get synced to both tenant and public schemas.
        """
        tenant = get_tenant_model()(schema_name='test')
        tenant.save()

        domain = get_tenant_domain_model()(tenant=tenant, domain='arbitrary.test.com')
        domain.save()

        shared_tables = self.get_tables_list_in_schema(get_public_schema_name())
        tenant_tables = self.get_tables_list_in_schema(tenant.schema_name)
        self.assertEqual(2 + 6 + 1 + 1 + self.MIGRATION_TABLE_SIZE, len(shared_tables))
        self.assertIn('django_session', shared_tables)
        self.assertEqual(1 + self.MIGRATION_TABLE_SIZE, len(tenant_tables))
        self.assertIn('django_session', tenant_tables)


class TestSyncTenantsNoAuth(BaseSyncTest):
    SHARED_APPS = ('django_tenants',  # 2 tables
                   'customers',
                   'django.contrib.contenttypes', )  # 1 table
    TENANT_APPS = ('django.contrib.sessions', )  # 1 table

    def test_content_types_is_not_mandatory(self):
        """
        Tests that even if content types is in SHARED_APPS, it's
        not required in TENANT_APPS.
        """
        tenant = get_tenant_model()(schema_name='test')
        tenant.save()
        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()

        shared_tables = self.get_tables_list_in_schema(get_public_schema_name())
        tenant_tables = self.get_tables_list_in_schema(tenant.schema_name)
        self.assertEqual(2 + 1 + self.MIGRATION_TABLE_SIZE, len(shared_tables))
        self.assertIn('django_session', tenant_tables)
        self.assertEqual(1 + self.MIGRATION_TABLE_SIZE, len(tenant_tables))
        self.assertIn('django_session', tenant_tables)


class SharedAuthTest(BaseTestCase):
    def setUp(self):
        super().setUp()

        settings.SHARED_APPS = ('django_tenants',
                                'customers',
                                'django.contrib.auth',
                                'django.contrib.contenttypes', )
        settings.TENANT_APPS = ('dts_test_app', )
        settings.INSTALLED_APPS = settings.SHARED_APPS + settings.TENANT_APPS
        self.sync_shared()
        self.public_tenant = get_tenant_model()(schema_name=get_public_schema_name())
        self.public_tenant.save()
        self.public_domain = get_tenant_domain_model()(tenant=self.public_tenant, domain='test.com')
        self.public_domain.save()

        # Create a tenant
        self.tenant = get_tenant_model()(schema_name='tenant')
        self.tenant.save()
        self.domain = get_tenant_domain_model()(tenant=self.tenant, domain='tenant.test.com')
        self.domain.save()

        # Create some users
        with schema_context(get_public_schema_name()):  # this could actually also be executed inside a tenant
            self.user1 = User(username='arbitrary-1', email="arb1@test.com")
            self.user1.save()
            self.user2 = User(username='arbitrary-2', email="arb2@test.com")
            self.user2.save()

        # Create instances on the tenant that point to the users on public
        with tenant_context(self.tenant):
            self.d1 = ModelWithFkToPublicUser(user=self.user1)
            self.d1.save()
            self.d2 = ModelWithFkToPublicUser(user=self.user2)
            self.d2.save()

    def tearDown(self):
        connection.set_schema_to_public()
        self.public_domain.delete()
        self.public_tenant.delete()
        self.domain.delete()
        self.tenant.delete(force_drop=True)

        super().tearDown()

    def test_cross_schema_constraint_gets_created(self):
        """
        Tests that a foreign key constraint gets created even for cross schema references.
        """
        sql = """
        SELECT
            tc.constraint_name, tc.table_name, kcu.column_name,
            ccu.table_name AS foreign_table_name,
            ccu.column_name AS foreign_column_name
        FROM
            information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_schema = kcu.constraint_schema AND tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage AS ccu
              ON tc.constraint_schema = ccu.constraint_schema AND ccu.constraint_name = tc.constraint_name
        WHERE
            constraint_type = 'FOREIGN KEY' AND
            tc.table_name=%s AND
            ccu.table_schema=%s AND
            ccu.constraint_schema=%s
        """
        cursor = connection.cursor()
        cursor.execute(
            sql,
            (
                ModelWithFkToPublicUser._meta.db_table,
                self.public_tenant.schema_name,
                self.tenant.schema_name
            )
        )
        fk_constraints = cursor.fetchall()
        self.assertEqual(1, len(fk_constraints))

        # The foreign key should reference the primary key of the user table
        fk = fk_constraints[0]
        self.assertEqual(User._meta.db_table, fk[3])
        self.assertEqual('id', fk[4])

    def test_direct_relation_to_public(self):
        """
        Tests that a forward relationship through a foreign key to public from a model inside TENANT_APPS works.
        """
        with tenant_context(self.tenant):
            self.assertEqual(User.objects.get(pk=self.user1.id),
                             ModelWithFkToPublicUser.objects.get(pk=self.d1.id).user)
            self.assertEqual(User.objects.get(pk=self.user2.id),
                             ModelWithFkToPublicUser.objects.get(pk=self.d2.id).user)

    def test_reverse_relation_to_public(self):
        """
        Tests that a reverse relationship through a foreign keys to public from a model inside TENANT_APPS works.
        """
        with tenant_context(self.tenant):
            users = User.objects.all().select_related().order_by('id')
            self.assertEqual(ModelWithFkToPublicUser.objects.get(pk=self.d1.id),
                             users[0].modelwithfktopublicuser_set.all()[:1].get())
            self.assertEqual(ModelWithFkToPublicUser.objects.get(pk=self.d2.id),
                             users[1].modelwithfktopublicuser_set.all()[:1].get())


class TenantTestCaseTest(BaseTestCase, TenantTestCase):
    """
    Tests that the tenant created inside TenantTestCase persists on
    all functions.
    """

    def test_tenant_survives_after_method1(self):
        # There is one tenant with schema name 'test' in the database, the one created by TenantTestCase
        self.assertEqual(1, get_tenant_model().objects.filter(
            schema_name=TenantTestCase.get_test_schema_name()
        ).count())

    def test_tenant_survives_after_method2(self):
        # The same tenant still exists even after the previous method call
        self.assertEqual(1, get_tenant_model().objects.filter(
            schema_name=TenantTestCase.get_test_schema_name()
        ).count())


class TenantManagerMethodsTestCaseTest(BaseTestCase):
    """
    Tests manager's delete method.
    """
    def test_manager_method_deletes_schema(self):
        Client = get_tenant_model()
        Client.auto_drop_schema = True
        tenant = Client(schema_name='test')
        tenant.save()
        self.assertTrue(schema_exists(tenant.schema_name))

        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()

        Client.objects.filter(pk=tenant.pk).delete()
        self.assertFalse(schema_exists(tenant.schema_name))


class TenantRenameSchemaTest(BaseTestCase):

    def test_rename_schema_ok(self):
        Client = get_tenant_model()
        tenant = Client(schema_name='test')
        tenant.save()
        self.assertTrue(schema_exists(tenant.schema_name))
        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()
        schema_rename(tenant=Client.objects.filter(pk=tenant.pk).first(), new_schema_name='new_name')
        self.assertFalse(schema_exists('test'))
        self.assertTrue(schema_exists('new_name'))

    def test_rename_schema_numbers(self):
        Client = get_tenant_model()
        tenant = Client(schema_name='1234_test')
        tenant.save()
        self.assertTrue(schema_exists(tenant.schema_name))
        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()
        schema_rename(tenant=Client.objects.filter(pk=tenant.pk).first(), new_schema_name='4321_new_name')
        self.assertFalse(schema_exists('1234_test'))
        self.assertTrue(schema_exists('4321_new_name'))

    def test_rename_schema_rejects_quotes(self):
        Client = get_tenant_model()
        tenant = Client(schema_name='rename_quotes')
        tenant.save()
        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()
        with self.assertRaises(ValidationError):
            schema_rename(tenant=Client.objects.filter(pk=tenant.pk).first(),
                          new_schema_name='new_name" RENAME TO "other')
        self.assertTrue(schema_exists('rename_quotes'))

        connection.set_schema_to_public()
        domain.delete()
        tenant.delete(force_drop=True)


class SchemaNameInjectionTest(BaseTestCase):

    def test_tenant_with_legacy_invalid_name_can_be_saved_and_renamed(self):
        # Names that later versions reject (here one containing '/') must not
        # lock the tenant out of unrelated saves, and rename_schema must still
        # work as the documented way to recover.
        tenant = get_tenant_model()(schema_name='legacy_slash')
        tenant.save()
        cursor = connection.cursor()
        cursor.execute('ALTER SCHEMA "legacy_slash" RENAME TO "legacy/slash"')
        get_tenant_model().objects.filter(pk=tenant.pk).update(schema_name='legacy/slash')
        tenant.refresh_from_db()
        try:
            tenant.name = 'unrelated change'
            tenant.save()
            schema_rename(tenant, 'legacy_fixed')
            self.assertTrue(schema_exists('legacy_fixed'))
            self.assertFalse(schema_exists('legacy/slash'))
            self.assertEqual(get_tenant_model().objects.get(pk=tenant.pk).schema_name, 'legacy_fixed')
        finally:
            connection.set_schema_to_public()
            tenant.delete(force_drop=True)

    def test_copy_made_by_clearing_pk_checks_case_insensitive_uniqueness(self):
        tenant = get_tenant_model()(schema_name='casecopy')
        tenant.save()
        try:
            copy = get_tenant_model().objects.get(pk=tenant.pk)
            copy.pk = None
            copy.schema_name = 'CASECOPY'
            copy.auto_create_schema = False
            with self.assertRaises(ValidationError):
                copy.save()
            self.assertEqual(get_tenant_model().objects.filter(schema_name__iexact='casecopy').count(), 1)
        finally:
            connection.set_schema_to_public()
            tenant.delete(force_drop=True)

    def test_multibyte_name_truncating_to_existing_schema_is_rejected(self):
        # PostgreSQL truncates identifiers to 63 bytes. 'victim' + 'b' is 64
        # bytes and would truncate to 'victim', giving the new tenant the
        # existing tenant's schema.
        victim_name = '\u00e9' * 31 + 'a'
        victim = get_tenant_model()(schema_name=victim_name)
        victim.save()
        domain = get_tenant_domain_model()(tenant=victim, domain='victim.test.com')
        domain.save()
        try:
            with self.assertRaises(ValidationError):
                get_tenant_model()(schema_name=victim_name + 'b').save()
            self.assertFalse(get_tenant_model().objects.filter(schema_name=victim_name + 'b').exists())
            # Rejecting the new tenant must not drop the existing tenant's schema.
            self.assertTrue(schema_exists(victim_name))

            # A tenant row that already has such a name (e.g. created before
            # upgrading) must not be able to drop the existing tenant's schema.
            attacker = get_tenant_model()(schema_name='placeholder')
            attacker.auto_create_schema = False
            attacker.save()
            get_tenant_model().objects.filter(pk=attacker.pk).update(schema_name=victim_name + 'b')
            attacker.refresh_from_db()
            with self.assertRaises(ValidationError):
                attacker.delete(force_drop=True)
            self.assertTrue(schema_exists(victim_name))
            get_tenant_model().objects.filter(pk=attacker.pk).delete()

            # Nor rename it: ALTER SCHEMA on the truncated name would rename
            # the existing tenant's schema to the attacker's new name.
            attacker = get_tenant_model()(schema_name='placeholder2')
            attacker.auto_create_schema = False
            attacker.save()
            get_tenant_model().objects.filter(pk=attacker.pk).update(schema_name=victim_name + 'b')
            attacker.refresh_from_db()
            with self.assertRaises(ValidationError):
                schema_rename(attacker, 'attacker_new_name')
            self.assertTrue(schema_exists(victim_name))
            self.assertFalse(schema_exists('attacker_new_name'))
            get_tenant_model().objects.filter(pk=attacker.pk).delete()

            # Copying a tenant row by clearing pk (clone_tenant
            # --clone_tenant_fields) is an insert and must be validated too.
            copy = get_tenant_model().objects.get(pk=victim.pk)
            copy.pk = None
            copy.schema_name = victim_name + 'b'
            copy.auto_create_schema = False
            with self.assertRaises(ValidationError):
                copy.save()

            # Renaming an existing tenant's schema_name through save() is
            # validated, and so is cloning into an invalid name.
            renamed = get_tenant_model().objects.get(pk=victim.pk)
            renamed.schema_name = victim_name + 'b'
            with self.assertRaises(ValidationError):
                renamed.save()
            with self.assertRaises(ValidationError):
                CloneSchema().clone_schema(victim_name, victim_name + 'b')
            self.assertTrue(schema_exists(victim_name))
        finally:
            connection.set_schema_to_public()
            domain.delete()
            victim.delete(force_drop=True)


    def test_search_path_rejects_injected_schema(self):
        tenant = get_tenant_model()(schema_name='tenant1')
        tenant.save()
        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()

        # Previously interpolated as SET search_path = 'tenant1', 'public', ...
        connection.set_schema("tenant1', 'public")
        try:
            with self.assertRaises(ValidationError):
                DummyModel.objects.count()
        finally:
            connection.set_schema_to_public()

    def test_create_schema_rejects_injected_sql(self):
        tenant = get_tenant_model()(schema_name='tenant1"; CREATE SCHEMA "injected')
        with self.assertRaises(ValidationError):
            tenant.save()
        self.assertFalse(schema_exists('injected'))


class CloneSchemaTest(BaseTestCase):
    def test_clone_schema(self):
        Client = get_tenant_model()
        tenant = Client(schema_name='source')
        tenant.save()
        self.assertTrue(schema_exists(tenant.schema_name))

        domain = get_tenant_domain_model()(tenant=tenant, domain='source.test.com')
        domain.save()
        clone_schema = CloneSchema()
        clone_schema.clone_schema(base_schema_name='source', new_schema_name='destination')

        self.assertTrue(schema_exists('source'))
        self.assertTrue(schema_exists('destination'))

    def test_clone_schema_with_existing_records_and_add_new_records_to_resulting_schema(self):
        """
        Exercises the scenario where the source schema contains records in a shared app which
        get cloned in the destination schema but the value of the PK column remains at 1 which
        causes duplicate key errors.
        See https://github.com/django-tenants/django-tenants/issues/831
        """
        Client = get_tenant_model()
        tenant = Client(schema_name='s1')
        tenant.save()

        domain = get_tenant_domain_model()(tenant=tenant, domain='s1.test.com')
        domain.save()
        self.assertTrue(schema_exists(tenant.schema_name))

        # add some records to the source schema
        with tenant_context(tenant):
            self.assertFalse(DummyModel.objects.filter(name='Administrator').exists())
            self.assertFalse(DummyModel.objects.filter(name='Tester').exists())
            DummyModel(name='Administrator').save()
            DummyModel(name='Tester').save()

        clone_schema = CloneSchema()
        clone_schema.clone_schema(base_schema_name='s1', new_schema_name='d1')
        self.assertTrue(schema_exists('destination'))

        # add some records to the destination schema
        with schema_context('d1'):
            self.assertTrue(DummyModel.objects.filter(name='Administrator').exists())
            self.assertTrue(DummyModel.objects.filter(name='Tester').exists())

            DummyModel(name='Moderator').save()


class SchemaMigratedSignalTest(BaseTestCase):

    def setUp(self):
        self.created = []
        super().setUp()
        self.sync_shared()

    def tearDown(self):
        from django_tenants.models import TenantMixin

        connection.set_schema_to_public()

        for c in self.created:
            if isinstance(c, TenantMixin):
                c.delete(force_drop=True)
            else:
                c.delete()

        super().tearDown()

    def test_signal_on_sync_shared(self):
        """
        Public schema always gets migrated in the current process,
        even with executor multiprocessing.
        """
        with catch_signal(schema_migrated) as handler_post, catch_signal(schema_pre_migration) as handler_pre:
            self.sync_shared()

        handler_pre.assert_called_once_with(
            schema_name=get_public_schema_name(),
            sender=mock.ANY,
            signal=schema_pre_migration,
        )
        handler_post.assert_called_once_with(
            schema_name=get_public_schema_name(),
            sender=mock.ANY,
            signal=schema_migrated,
        )

    def test_signal_on_tenant_create(self):
        """
        Since migrate gets called on creating of a tenant, check
        the signal gets sent.
        """
        executor = get_executor()

        tenant = get_tenant_model()(schema_name='test')
        with catch_signal(schema_migrated) as handler_post, catch_signal(schema_pre_migration) as handler_pre:
            tenant.save()

        if executor == 'simple':
            handler_pre.assert_called_once_with(
                schema_name='test',
                sender=mock.ANY,
                signal=schema_pre_migration
            )
            handler_post.assert_called_once_with(
                schema_name='test',
                sender=mock.ANY,
                signal=schema_migrated
            )
        elif executor == 'multiprocessing':
            # migrations run in a different process, therefore signal
            # will get sent in a different process as well
            handler_post.assert_not_called()
            handler_pre.assert_not_called()

        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()
        self.created = [domain, tenant]

    def test_signal_on_migrate_schemas(self):
        """
        Check signals are sent on running of migrate_schemas.
        """
        executor = get_executor()
        tenant = get_tenant_model()(schema_name='test')
        tenant.save()
        domain = get_tenant_domain_model()(tenant=tenant, domain='something.test.com')
        domain.save()

        # test the signal gets called when running migrate
        with catch_signal(schema_migrated) as handler_post, catch_signal(schema_pre_migration) as handler_pre:
            call_command('migrate_schemas', interactive=False, verbosity=0)

        if executor == 'simple':
            handler_pre.assert_has_calls([
                mock.call(
                    schema_name=get_public_schema_name(),
                    sender=mock.ANY,
                    signal=schema_pre_migration,
                ),
                mock.call(
                    schema_name='test',
                    sender=mock.ANY,
                    signal=schema_pre_migration)
            ])
            handler_post.assert_has_calls([
                mock.call(
                    schema_name=get_public_schema_name(),
                    sender=mock.ANY,
                    signal=schema_migrated,
                ),
                mock.call(
                    schema_name='test',
                    sender=mock.ANY,
                    signal=schema_migrated)
            ])
        elif executor == 'multiprocessing':
            # public schema gets migrated in the current process, always
            handler_pre.assert_called_once_with(
                schema_name=get_public_schema_name(),
                sender=mock.ANY,
                signal=schema_pre_migration,
            )
            handler_post.assert_called_once_with(
                schema_name=get_public_schema_name(),
                sender=mock.ANY,
                signal=schema_migrated,
            )


class MigrationOrderTestTest(BaseTestCase):
    def setUp(self):
        self.created = []
        super().setUp()

        settings.TENANT_MIGRATION_ORDER = ["-schema_name"]

        self.sync_shared()

    def tearDown(self):
        from django_tenants.models import TenantMixin

        connection.set_schema_to_public()

        for c in self.created:
            if isinstance(c, TenantMixin):
                c.delete(force_drop=True)
            else:
                c.delete()

        settings.TENANT_MIGRATION_ORDER = None

        super().tearDown()

    def test_migrate_schemas_order(self):
        """
        Test the migrate schemas is determined by TENANT_MIGRATION_ORDER.
        """
        executor = get_executor()
        if executor.codename != "standard":  # can only test in standard executor
            return

        tenant1 = get_tenant_model().objects.create(schema_name="test")
        tenant2 = get_tenant_model().objects.create(schema_name="xtest")
        get_tenant_domain_model().objects.create(
            tenant=tenant1, domain="something1.test.com"
        )
        get_tenant_domain_model().objects.create(
            tenant=tenant2, domain="something2.test.com"
        )

        # test the signal gets called when running migrate
        with catch_signal(schema_migrated) as handler_post, catch_signal(schema_pre_migration) as handler_pre:
            call_command("migrate_schemas", interactive=False, verbosity=0)

        handler_pre.assert_has_calls(
            [
                mock.call(
                    schema_name=get_public_schema_name(),
                    sender=mock.ANY,
                    signal=schema_pre_migration,
                ),
                mock.call(schema_name="xtest", sender=mock.ANY, signal=schema_pre_migration),
                mock.call(schema_name="test", sender=mock.ANY, signal=schema_pre_migration),
            ]
        )

        handler_post.assert_has_calls(
            [
                mock.call(
                    schema_name=get_public_schema_name(),
                    sender=mock.ANY,
                    signal=schema_migrated,
                ),
                mock.call(schema_name="xtest", sender=mock.ANY, signal=schema_migrated),
                mock.call(schema_name="test", sender=mock.ANY, signal=schema_migrated),
            ]
        )


class SchemaAdoptionTest(BaseTestCase):
    """A tenant must not be able to take over a schema that isn't its own."""

    def _drop(self, tenant):
        connection.set_schema_to_public()
        get_tenant_model()._base_manager.filter(pk=tenant.pk).delete()

    def _create_plain_schema(self, name):
        cursor = connection.cursor()
        cursor.execute(sql.SQL('CREATE SCHEMA {}').format(quote_schema_name(name)))
        cursor.execute(sql.SQL('CREATE TABLE {}.secret (value text)').format(
            quote_schema_name(name)))
        self.addCleanup(lambda: connection.cursor().execute(
            sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(quote_schema_name(name))))

    def test_information_schema_is_reserved(self):
        for schema_name in ('information_schema', 'Information_Schema'):
            with self.subTest(schema_name=schema_name):
                with self.assertRaisesRegex(ValidationError, 'reserved'):
                    get_tenant_model()(schema_name=schema_name).save()
                self.assertFalse(get_tenant_model().objects.filter(schema_name=schema_name).exists())
        self.assertTrue(schema_exists('information_schema'))

    def test_extra_search_path_schema_is_reserved(self):
        self._create_plain_schema('shared_ext')
        with override_settings(PG_EXTRA_SEARCH_PATHS=['shared_ext']):
            with self.assertRaisesRegex(ValidationError, 'reserved'):
                get_tenant_model()(schema_name='shared_ext').save()

            # Nor can an existing tenant be renamed to it by saving.
            tenant = get_tenant_model()(schema_name='adopt_rename')
            tenant.auto_create_schema = False
            tenant.save()
            self.addCleanup(self._drop, tenant)
            tenant.schema_name = 'shared_ext'
            with self.assertRaisesRegex(ValidationError, 'reserved'):
                tenant.save()

            # And a tenant row that already has the name cannot drop it: the
            # delete is refused and nothing is deleted.
            get_tenant_model().objects.filter(pk=tenant.pk).update(schema_name='shared_ext')
            tenant.refresh_from_db()
            with self.assertRaisesRegex(ValidationError, 'never dropped'):
                tenant.delete(force_drop=True)
            self.assertTrue(get_tenant_model().objects.filter(pk=tenant.pk).exists())
            self.assertTrue(schema_exists('shared_ext'))
            # Deleting only the row, without dropping, still works.
            tenant.delete()
            self.assertFalse(get_tenant_model().objects.filter(pk=tenant.pk).exists())
        self.assertTrue(schema_exists('shared_ext'))

    def test_reject_existing_schema_setting(self):
        self._create_plain_schema('other_app')

        # Off by default: a tenant can still be created for an existing schema
        # (e.g. one restored from a backup).
        tenant = get_tenant_model()(schema_name='other_app')
        tenant.auto_create_schema = False
        tenant.save()
        self._drop(tenant)

        with override_settings(TENANT_REJECT_EXISTING_SCHEMA=True):
            for schema_name in ('other_app', 'OTHER_APP'):
                with self.subTest(schema_name=schema_name):
                    with self.assertRaisesRegex(ValidationError, 'already exists'):
                        get_tenant_model()(schema_name=schema_name).save()

            tenant = get_tenant_model()(schema_name='adopt_by_save')
            tenant.save()
            self.addCleanup(lambda: get_tenant_model().objects.get(pk=tenant.pk).delete(force_drop=True))
            tenant.schema_name = 'other_app'
            with self.assertRaisesRegex(ValidationError, 'already exists'):
                tenant.save()
        self.assertTrue(schema_exists('other_app'))
        self.assertEqual(get_tenant_model().objects.get(pk=tenant.pk).schema_name, 'adopt_by_save')

    def test_rename_by_save_checks_case_insensitive_uniqueness(self):
        first = get_tenant_model()(schema_name='casefirst')
        first.auto_create_schema = False
        first.save()
        self.addCleanup(self._drop, first)
        second = get_tenant_model()(schema_name='casesecond')
        second.auto_create_schema = False
        second.save()
        self.addCleanup(self._drop, second)

        second.schema_name = 'CASEFIRST'
        with self.assertRaisesRegex(ValidationError, 'already exists'):
            second.save()

        # Changing only the case of its own name by saving is rejected too: the
        # row would point at a schema that doesn't exist.
        second.schema_name = 'CaseSecond'
        with self.assertRaisesRegex(ValidationError, 'already exists'):
            second.save()

        # Saving a tenant without changing its name is still allowed, even if
        # it already collides (so upgrading doesn't lock anyone out).
        get_tenant_model().objects.filter(pk=second.pk).update(schema_name='CASEFIRST')
        second.refresh_from_db()
        second.name = 'unrelated change'
        second.save()

    def test_schema_rename_works_with_reject_existing_schema(self):
        tenant = get_tenant_model()(schema_name='rename_setting_a')
        tenant.save()
        self.addCleanup(lambda: get_tenant_model().objects.get(pk=tenant.pk).delete(force_drop=True))
        with override_settings(TENANT_REJECT_EXISTING_SCHEMA=True):
            schema_rename(tenant, 'rename_setting_b')
        self.assertEqual(get_tenant_model().objects.get(pk=tenant.pk).schema_name, 'rename_setting_b')
        self.assertTrue(schema_exists('rename_setting_b'))
        self.assertFalse(schema_exists('rename_setting_a'))

    def test_schema_rename_to_reserved_name_is_rejected_before_renaming(self):
        tenant = get_tenant_model()(schema_name='rename_reserved')
        tenant.save()
        self.addCleanup(lambda: get_tenant_model().objects.get(pk=tenant.pk).delete(force_drop=True))
        with override_settings(PG_EXTRA_SEARCH_PATHS=['not_created_yet']):
            for save in (True, False):
                with self.subTest(save=save):
                    with self.assertRaisesRegex(ValidationError, 'reserved'):
                        schema_rename(tenant, 'not_created_yet', save=save)
        self.assertTrue(schema_exists('rename_reserved'))
        self.assertFalse(schema_exists('not_created_yet'))
        self.assertEqual(tenant.schema_name, 'rename_reserved')

    def test_schema_rename_is_rolled_back_if_save_fails(self):
        tenant = get_tenant_model()(schema_name='rename_rollback')
        tenant.save()
        self.addCleanup(lambda: get_tenant_model().objects.get(pk=tenant.pk).delete(force_drop=True))
        with mock.patch.object(tenant, 'save', side_effect=ValidationError('boom')):
            with self.assertRaises(ValidationError):
                schema_rename(tenant, 'rename_rollback_new')
        self.assertEqual(tenant.schema_name, 'rename_rollback')
        self.assertFalse(hasattr(tenant, '_renamed_to_schema_name'))
        self.assertTrue(schema_exists('rename_rollback'))
        self.assertFalse(schema_exists('rename_rollback_new'))

    def test_public_tenant_name_is_allowed_with_reject_existing_schema(self):
        with override_settings(TENANT_REJECT_EXISTING_SCHEMA=True):
            self.assertIsNone(
                get_tenant_model()(schema_name=get_public_schema_name())._check_schema_name_is_available())

    def test_public_schema_is_never_dropped_or_renamed_through_a_tenant(self):
        tenant = get_tenant_model()(schema_name='holds_public')
        tenant.auto_create_schema = False
        tenant.save()
        self.addCleanup(self._drop, tenant)
        get_tenant_model().objects.filter(pk=tenant.pk).update(schema_name=get_public_schema_name())
        tenant.refresh_from_db()
        with self.assertRaisesRegex(ValidationError, 'reserved'):
            schema_rename(tenant, 'moved_public', save=False)
        self.assertFalse(schema_exists('moved_public'))
        # Deleting it with force_drop, or via a queryset with
        # auto_drop_schema, is refused and deletes nothing.
        with self.assertRaisesRegex(ValidationError, 'never dropped'):
            tenant.delete(force_drop=True)
        self.assertTrue(get_tenant_model().objects.filter(pk=tenant.pk).exists())
        self.assertTrue(schema_exists(get_public_schema_name()))
        with mock.patch.object(get_tenant_model(), 'auto_drop_schema', True):
            with self.assertRaisesRegex(ValidationError, 'never dropped'):
                with transaction.atomic():
                    get_tenant_model().objects.filter(pk=tenant.pk).delete()
        self.assertTrue(get_tenant_model().objects.filter(pk=tenant.pk).exists())
        self.assertTrue(schema_exists(get_public_schema_name()))
        # Deleting only the row, without dropping, still works.
        tenant.delete()
        self.assertFalse(get_tenant_model().objects.filter(pk=tenant.pk).exists())

        # NOTE: unlike upstream 4.x, this fork's schema_exists() is still
        # case-insensitive (the 4.0.0 change was not backported), so "Public"
        # is not a separate schema here and this case is not exercised.

    def test_failed_public_tenant_create_keeps_schema_and_original_error(self):
        # Cleaning up after a failed create must not drop the public schema,
        # nor hide the original error by refusing.
        get_tenant_model().objects.filter(schema_name=get_public_schema_name()).update(
            schema_name='public_was_here')
        self.addCleanup(lambda: get_tenant_model().objects.filter(
            schema_name='public_was_here').update(schema_name=get_public_schema_name()))

        def fail(**kwargs):
            raise RuntimeError('original failure')
        post_schema_sync.connect(fail, dispatch_uid='test_failed_public_create')
        self.addCleanup(post_schema_sync.disconnect, dispatch_uid='test_failed_public_create')

        tenant = get_tenant_model()(schema_name=get_public_schema_name())
        with self.assertRaisesRegex(RuntimeError, 'original failure'):
            tenant.save()
        self.assertFalse(get_tenant_model().objects.filter(schema_name=get_public_schema_name()).exists())
        self.assertTrue(schema_exists(get_public_schema_name()))
        self.assertFalse(hasattr(tenant, '_in_failed_create_cleanup'))

    def test_failed_create_never_drops_a_schema_it_did_not_create(self):
        self._create_plain_schema('adopted_then_failed')

        def fail(**kwargs):
            raise RuntimeError('original failure')
        post_schema_sync.connect(fail, dispatch_uid='test_failed_adopt')
        self.addCleanup(post_schema_sync.disconnect, dispatch_uid='test_failed_adopt')

        # The schema already existed: the row is removed, the schema is kept.
        tenant = get_tenant_model()(schema_name='adopted_then_failed')
        with self.assertRaisesRegex(RuntimeError, 'original failure'):
            tenant.save()
        self.assertFalse(get_tenant_model().objects.filter(schema_name='adopted_then_failed').exists())
        self.assertTrue(schema_exists('adopted_then_failed'))
        self.assertFalse(hasattr(tenant, '_in_failed_create_cleanup'))

        # A schema this save created is still cleaned up.
        tenant = get_tenant_model()(schema_name='created_then_failed')
        with self.assertRaisesRegex(RuntimeError, 'original failure'):
            tenant.save()
        self.assertFalse(get_tenant_model().objects.filter(schema_name='created_then_failed').exists())
        self.assertFalse(schema_exists('created_then_failed'))

    def test_delete_tenant_command_refuses_the_public_tenant(self):
        tenant = get_tenant_model()(schema_name='cmd_holds_public')
        tenant.auto_create_schema = False
        tenant.save()
        self.addCleanup(self._drop, tenant)
        get_tenant_model().objects.filter(pk=tenant.pk).update(schema_name=get_public_schema_name())
        with self.assertRaisesRegex(CommandError, 'never dropped'):
            call_command('delete_tenant', schema_name=get_public_schema_name(), interactive=False)
        self.assertTrue(get_tenant_model().objects.filter(pk=tenant.pk).exists())
        self.assertTrue(schema_exists(get_public_schema_name()))

    def test_schema_rename_from_reserved_name_is_rejected(self):
        self._create_plain_schema('shared_ext2')
        tenant = get_tenant_model()(schema_name='holds_ext')
        tenant.auto_create_schema = False
        tenant.save()
        self.addCleanup(self._drop, tenant)
        get_tenant_model().objects.filter(pk=tenant.pk).update(schema_name='shared_ext2')
        tenant.refresh_from_db()
        with override_settings(PG_EXTRA_SEARCH_PATHS=['shared_ext2']):
            with self.assertRaisesRegex(ValidationError, 'reserved'):
                schema_rename(tenant, 'moved_ext')
        self.assertTrue(schema_exists('shared_ext2'))
        self.assertFalse(schema_exists('moved_ext'))

    def test_schema_rename_without_save_then_save_with_reject_existing_schema(self):
        tenant = get_tenant_model()(schema_name='rename_nosave_a')
        tenant.save()
        self.addCleanup(lambda: get_tenant_model().objects.get(pk=tenant.pk).delete(force_drop=True))
        with override_settings(TENANT_REJECT_EXISTING_SCHEMA=True):
            schema_rename(tenant, 'rename_nosave_b', save=False)
            tenant.save()
        self.assertEqual(get_tenant_model().objects.get(pk=tenant.pk).schema_name, 'rename_nosave_b')
